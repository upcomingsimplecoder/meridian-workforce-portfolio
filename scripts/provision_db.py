"""Operator-only, versioned runtime grants. No admin credentials enter app settings."""

import argparse
import json
import os
import secrets
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg import sql
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
ROLE_NAMES = {"api": "meridian_app", "worker": "meridian_worker", "identity": "meridian_identity"}
READ_TABLES = set(
    """alembic_version analytics_artifact analytics_delivery analytics_publication analytics_result analytics_run audit_event authz_policy_version calibration_event comp_approval_step comp_cycle comp_cycle_action comp_decision comp_eligibility comp_export_artifact comp_guideline_version comp_lock comp_pool comp_proposal comp_proposal_revision comp_reconciliation comp_review_round comp_revision_verification consumer_receipt cost_policy_version employee employee_version fx_rate fx_rate_set idempotency_receipt ingestion_error ingestion_input ingestion_manifest ingestion_record org_unit org_version outbox_event outbox_trace trace_span packet_revision planning_baseline position principal principal_employee replay_artifact replay_attempt replay_capsule review_packet role_assignment scenario scenario_decision scenario_forecast scenario_manifest scenario_revision scenario_verification snapshot snapshot_publication source_batch talent_comment talent_cycle talent_cycle_action talent_decision talent_export talent_lock talent_policy_version upload_intent worker_heartbeat""".split()
)
APP_INSERT = set(
    """analytics_run audit_event calibration_event comp_approval_step comp_cycle comp_cycle_action comp_decision comp_eligibility comp_lock comp_pool comp_proposal comp_proposal_revision comp_reconciliation comp_review_round employee employee_version idempotency_receipt ingestion_input login_state org_version outbox_event outbox_trace trace_span packet_revision planning_baseline position replay_artifact replay_attempt replay_capsule review_packet scenario scenario_decision scenario_forecast scenario_revision snapshot snapshot_publication source_batch talent_comment talent_cycle talent_cycle_action talent_decision talent_lock upload_intent web_session""".split()
)
APP_UPDATE = {
    "comp_cycle": None,
    "comp_pool": None,
    "comp_proposal": None,
    "ingestion_input": ("version",),
    "login_state": ("state_hash",),
    "outbox_event": ("state", "attempts", "next_attempt_at", "last_error"),
    "replay_capsule": ("id",),  # Row lock requires UPDATE; immutable trigger still rejects writes.
    "review_packet": None,
    "scenario": None,
    "scenario_revision": ("id",),  # Evidence trigger FOR SHARE; immutable trigger rejects writes.
    "snapshot": ("status",),
    "source_batch": ("state", "published_at"),
    "talent_cycle": None,
    "upload_intent": None,
    "web_session": ("revoked",),
}
WORKER_INSERT = set(
    """analytics_artifact analytics_delivery analytics_publication analytics_result audit_event comp_cycle_action comp_export_artifact comp_revision_verification consumer_receipt ingestion_error ingestion_manifest ingestion_record outbox_event scenario_manifest scenario_verification talent_export trace_span worker_heartbeat""".split()
)
WORKER_UPDATE = {
    "comp_cycle": ("state", "version"),
    "ingestion_input": ("total_count", "validated_at", "version"),
    "outbox_event": ("state", "attempts", "next_attempt_at", "last_error"),
    "scenario": ("id",),  # Evidence trigger FOR SHARE; pinned-state trigger rejects ID changes.
    "scenario_revision": ("id",),
    "source_batch": ("accepted_count", "rejected_count", "state"),
    "worker_heartbeat": ("last_seen_at",),
}
KNOWN_TABLES = READ_TABLES | {"login_state", "web_session", "demo_credential", "oidc_record"}


def grant(db, role, verb, tables):
    if tables:
        db.execute(
            sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                sql.SQL(verb),
                sql.SQL(",").join(sql.Identifier("public", name) for name in sorted(tables)),
                sql.Identifier(role),
            )
        )


def update_grants(db, role, mapping):
    for table, columns in mapping.items():
        privilege = (
            sql.SQL("UPDATE")
            if columns is None
            else sql.SQL("UPDATE ({})").format(sql.SQL(",").join(map(sql.Identifier, columns)))
        )
        db.execute(
            sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                privilege, sql.Identifier("public", table), sql.Identifier(role)
            )
        )


def harden_supabase(db, tables):
    """Keep PostgREST roles outside the Meridian data boundary."""
    db.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC")
    db.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC")
    db.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC")
    db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC")
    db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC")
    db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM PUBLIC")
    for role in ("anon", "authenticated", "service_role"):
        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            continue
        identifier = sql.Identifier(role)
        for resource in ("TABLES", "SEQUENCES", "FUNCTIONS"):
            db.execute(
                sql.SQL("REVOKE ALL ON ALL {} IN SCHEMA public FROM {}").format(
                    sql.SQL(resource), identifier
                )
            )
            db.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON {} FROM {}"
                ).format(sql.SQL(resource), identifier)
            )
        db.execute(sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(identifier))
    roles = sql.SQL(",").join(sql.Identifier(role) for role in ROLE_NAMES.values())
    for table in sorted(tables):
        identifier = sql.Identifier("public", table)
        db.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(identifier))
        # New projects have no business policies. Refuse a permissive policy
        # from a prior deployment rather than silently retaining it.
        policies = db.execute(
            "SELECT policyname FROM pg_policies WHERE schemaname='public' AND tablename=%s",
            (table,),
        ).fetchall()
        if any(row[0] != "meridian_trusted_sql" for row in policies):
            raise RuntimeError("Unexpected business-table policy; operator review required")
        db.execute(sql.SQL("DROP POLICY IF EXISTS meridian_trusted_sql ON {}").format(identifier))
        db.execute(
            sql.SQL(
                "CREATE POLICY meridian_trusted_sql ON {} TO {} USING (true) WITH CHECK (true)"
            ).format(identifier, roles)
        )


def provision(admin, passwords, *, supabase=False):
    uri = make_url(admin).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(uri) as db:
        database = db.execute("SELECT current_database()").fetchone()[0]
        tables = {
            row[0]
            for row in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        }
        if tables != KNOWN_TABLES:
            raise RuntimeError("Schema must match the explicitly reviewed runtime grant inventory")
        if supabase:
            # Platform schema readers can otherwise deadlock with role/catalog
            # changes followed by RLS DDL. Acquire business-table locks first,
            # in a stable order, before modifying any role or grant. This is an
            # operator maintenance transaction, never a runtime request.
            db.execute(
                sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
                    sql.SQL(",").join(sql.Identifier("public", name) for name in sorted(tables))
                )
            )
        db.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        # Supabase manages internal Auth/Storage connections. Keep their
        # CONNECT inheritance, while denying all Meridian object privileges.
        database_privilege = "TEMPORARY" if supabase else "CONNECT, TEMPORARY"
        db.execute(
            sql.SQL("REVOKE {} ON DATABASE {} FROM PUBLIC").format(
                sql.SQL(database_privilege), sql.Identifier(database)
            )
        )
        for role in ROLE_NAMES.values():
            exists = db.execute(
                "SELECT rolsuper,rolcreaterole,rolcreatedb,rolbypassrls FROM pg_roles WHERE rolname=%s",
                (role,),
            ).fetchone()
            if exists is None:
                db.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                    ).format(sql.Identifier(role), sql.Literal(passwords[role]))
                )
            elif any(exists):
                raise RuntimeError("Runtime role has unexpected elevated privileges")
            memberships = db.execute(
                "SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=%s)",
                (role,),
            ).fetchone()
            if memberships:
                raise RuntimeError("Runtime role cannot inherit another role")
            db.execute(
                sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(
                    sql.Identifier(role)
                )
            )
            db.execute(
                sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(
                    sql.Identifier(role)
                )
            )
            # Table-level REVOKE does not remove existing column ACLs. Reapply
            # an exact policy, including on upgrades that remove a column grant.
            for table in sorted(tables):
                columns = [
                    row[0]
                    for row in db.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s",
                        (table,),
                    )
                ]
                names = sql.SQL(",").join(map(sql.Identifier, columns))
                privileges = sql.SQL(",").join(
                    sql.SQL("{} ({})").format(sql.SQL(verb), names)
                    for verb in ("SELECT", "INSERT", "UPDATE", "REFERENCES")
                )
                db.execute(
                    sql.SQL("REVOKE {} ON TABLE {} FROM {}").format(
                        privileges, sql.Identifier("public", table), sql.Identifier(role)
                    )
                )
            db.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
            db.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            db.execute(
                sql.SQL("ALTER ROLE {} SET search_path TO pg_catalog,public").format(
                    sql.Identifier(role)
                )
            )
        app, worker, identity = ROLE_NAMES.values()
        grant(db, app, "SELECT", READ_TABLES | {"login_state", "web_session"})
        grant(db, app, "INSERT", APP_INSERT)
        grant(db, app, "DELETE", {"login_state"})
        update_grants(db, app, APP_UPDATE)
        db.execute(
            sql.SQL("GRANT USAGE ON SEQUENCE public.snapshot_publication_ordinal_seq TO {}").format(
                sql.Identifier(app)
            )
        )
        grant(db, worker, "SELECT", READ_TABLES)
        grant(db, worker, "INSERT", WORKER_INSERT)
        update_grants(db, worker, WORKER_UPDATE)
        grant(db, identity, "SELECT", {"principal", "demo_credential", "oidc_record"})
        grant(db, identity, "INSERT, UPDATE, DELETE", {"oidc_record"})
        if supabase:
            harden_supabase(db, tables)


def main():
    from scripts.operator_db import admin_url

    parser = argparse.ArgumentParser()
    parser.add_argument("--no-env", action="store_true")
    parser.add_argument("--test-db", action="store_true")
    parser.add_argument("--supabase", action="store_true")
    args = parser.parse_args()
    admin = admin_url()
    if args.supabase:
        target = make_url(admin)
        if (
            not args.no_env
            or not os.environ.get("MIGRATION_DATABASE_URL")
            or target.query.get("sslmode") != "verify-full"
            or not (target.host or "").endswith((".supabase.co", ".pooler.supabase.com"))
        ):
            raise RuntimeError(
                "Supabase provisioning requires an explicit TLS operator URL and --no-env"
            )
    directory = ROOT / ".local"
    secrets_path = directory / "runtime-database-credentials.json"
    supplied = {
        role: os.environ.get("MERIDIAN_" + name.upper() + "_DB_PASSWORD")
        for name, role in ROLE_NAMES.items()
    }
    if any(supplied.values()) and not all(supplied.values()):
        raise RuntimeError("All three runtime passwords must be supplied together")
    if all(supplied.values()):
        passwords = supplied
    elif secrets_path.exists():
        if secrets_path.is_symlink() or secrets_path.stat().st_mode & 0o077:
            raise RuntimeError("Runtime credentials must be a private 0600 file")
        passwords = json.loads(secrets_path.read_text())
    else:
        passwords = {name: secrets.token_urlsafe(32) for name in ROLE_NAMES.values()}
        secrets_path.write_text(json.dumps(passwords) + "\n")
        secrets_path.chmod(0o600)
    provision(admin, passwords, supabase=args.supabase)
    target = make_url(admin)
    # Integration test roles are provisioned only for an isolated local test DB.
    if args.test_db and target.host in {
        "127.0.0.1",
        "localhost",
        "postgres",
    }:
        provision(
            target.set(database="meridian_test").render_as_string(hide_password=False), passwords
        )
    # Verify every configured login before replacing any runtime configuration.
    for role in ROLE_NAMES.values():
        uri = target.set(drivername="postgresql", username=role, password=passwords[role])
        with psycopg.connect(uri.render_as_string(hide_password=False)) as db:
            db.execute("SELECT 1")
    if args.no_env:
        print("Explicit runtime grants applied; environment and source data retained.")
        return
    values = dotenv_values(ROOT / ".env")
    for key, name in [
        ("DATABASE_URL", "api"),
        ("WORKER_DATABASE_URL", "worker"),
        ("OIDC_DATABASE_URL", "identity"),
    ]:
        url = target.set(username=ROLE_NAMES[name], password=passwords[ROLE_NAMES[name]])
        if name == "identity":
            url = url.set(drivername="postgresql")
        values[key] = url.render_as_string(hide_password=False)
    (ROOT / ".env").write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    (ROOT / ".env").chmod(0o600)
    print(
        "Provisioned explicit api/worker/identity grants; operator credential retained privately."
    )


if __name__ == "__main__":
    main()
