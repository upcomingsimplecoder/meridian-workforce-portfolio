"""Actual PostgreSQL login roles, not emulated permission predicates."""

import json
import secrets
from pathlib import Path

import psycopg
import pytest
from meridian.finance_seed import FX_BASE_ID, POLICY_ID
from meridian.models import uid
from meridian.people_seed import CURRENT_SNAPSHOT
from meridian.scenario_models import Scenario
from meridian.scenario_operations import PlanningRules
from meridian.scenario_service import save_revision
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from scripts.operator_db import test_url as isolated_url
from scripts.provision_db import ROLE_NAMES, provision


class PrivateConnections(dict):
    def __repr__(self):
        return "<private runtime database connections>"


@pytest.fixture(scope="module")
def role_urls():
    admin = isolated_url()
    path = Path(".local/runtime-database-credentials.json")
    if path.exists():
        passwords = json.loads(path.read_text())
    else:
        passwords = {name: secrets.token_urlsafe(32) for name in ROLE_NAMES.values()}
        path.write_text(json.dumps(passwords) + "\n")
        path.chmod(0o600)
    provision(admin, passwords)
    return PrivateConnections(
        {
            name: make_url(admin)
            .set(drivername="postgresql", username=role, password=passwords[role])
            .render_as_string(hide_password=False)
            for name, role in ROLE_NAMES.items()
        }
    )


@pytest.mark.security
@pytest.mark.parametrize("name", ["api", "worker", "identity"])
def test_runtime_login_has_no_owner_admin_or_grant_escalation(role_urls, name):
    with psycopg.connect(role_urls[name]) as db:
        row = db.execute(
            "SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        assert row is not None and not any(row)
        assert (
            db.execute(
                "SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user)"
            ).fetchone()[0]
            == 0
        )
        for statement in (
            "CREATE TABLE public.privilege_escape (id integer)",
            "ALTER TABLE public.audit_event DISABLE TRIGGER ALL",
            "TRUNCATE public.audit_event",
            "DELETE FROM public.audit_event WHERE false",
            "UPDATE public.role_assignment SET scope='*' WHERE false",
            "SET ROLE meridian",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with db.transaction():
                    db.execute(statement)


@pytest.mark.security
@pytest.mark.parametrize("name", ["api", "worker"])
def test_api_and_worker_cannot_read_identity_secrets(role_urls, name):
    with psycopg.connect(role_urls[name]) as db:
        for table in ("demo_credential", "oidc_record"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with db.transaction():
                    db.execute(
                        psycopg.sql.SQL("SELECT * FROM {} LIMIT 1").format(
                            psycopg.sql.Identifier(table)
                        )
                    )
        assert db.execute("SELECT count(*) FROM employee_version").fetchone()[0] == 10002


@pytest.mark.security
def test_identity_cannot_read_workforce_or_web_session_or_write_principal(role_urls):
    with psycopg.connect(role_urls["identity"]) as db:
        assert db.execute("SELECT count(*) FROM principal").fetchone()[0] == 8
        for statement in (
            "SELECT annual_pay_minor FROM employee_version LIMIT 1",
            "SELECT * FROM web_session LIMIT 1",
            "UPDATE principal SET active=true WHERE false",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with db.transaction():
                    db.execute(statement)
        with db.transaction(force_rollback=True):
            db.execute(
                "INSERT INTO oidc_record(model,id,payload) VALUES ('PrivilegeTest',%s,'{}')",
                (secrets.token_hex(16),),
            )


@pytest.mark.security
def test_worker_cannot_mint_sessions_or_decisions_or_rewrite_event_pins(role_urls):
    with psycopg.connect(role_urls["worker"]) as db:
        for statement in (
            "SELECT * FROM web_session LIMIT 1",
            "SELECT * FROM login_state LIMIT 1",
            "UPDATE outbox_event SET payload='{}' WHERE false",
            "INSERT INTO scenario_decision SELECT * FROM scenario_decision WHERE false",
            "INSERT INTO comp_decision SELECT * FROM comp_decision WHERE false",
            "INSERT INTO talent_decision SELECT * FROM talent_decision WHERE false",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with db.transaction():
                    db.execute(statement)
        # Operational retry fields remain writable; immutable payload fields do not.
        db.execute("UPDATE outbox_event SET attempts=0 WHERE false")


@pytest.mark.security
def test_api_cannot_forge_worker_verifications_or_exports(role_urls):
    with psycopg.connect(role_urls["api"]) as db:
        for table in (
            "consumer_receipt",
            "scenario_verification",
            "scenario_manifest",
            "comp_revision_verification",
            "comp_export_artifact",
            "talent_export",
            "analytics_result",
            "ingestion_record",
            "ingestion_error",
            "worker_heartbeat",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with db.transaction():
                    db.execute(
                        psycopg.sql.SQL("INSERT INTO {} SELECT * FROM {} WHERE false").format(
                            psycopg.sql.Identifier(table), psycopg.sql.Identifier(table)
                        )
                    )
        # The locked replay capsule is immutable, but selecting its row lock is allowed.
        db.execute("SELECT id FROM replay_capsule FOR UPDATE")


@pytest.mark.security
@pytest.mark.parametrize(
    "name,service,kind",
    [("api", "meridian-worker", "CONSUMER"), ("worker", "meridian-api", "SERVER")],
)
def test_database_rejects_trace_service_impersonation_and_technical_rewrite(
    role_urls, name, service, kind
):
    with psycopg.connect(role_urls[name]) as db:
        with pytest.raises(psycopg.errors.RaiseException, match="impersonate"):
            with db.transaction():
                db.execute(
                    "INSERT INTO trace_span(span_id,trace_id,request_id,service,kind,outcome,started_at,duration_ms) VALUES (%s,%s,%s,%s,%s,'OK',now(),1)",
                    (secrets.token_hex(8), secrets.token_hex(16), uid(), service, kind),
                )
        for statement in (
            "UPDATE trace_span SET outcome='OK' WHERE false",
            "DELETE FROM trace_span WHERE false",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with db.transaction():
                    db.execute(statement)
        if name == "worker":
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with db.transaction():
                    db.execute("INSERT INTO outbox_trace SELECT * FROM outbox_trace WHERE false")


@pytest.mark.security
@pytest.mark.parametrize("name", ["api", "worker"])
def test_evidence_row_locks_do_not_permit_revision_mutation(role_urls, name):
    with psycopg.connect(role_urls[name]) as db:
        db.execute("SELECT id FROM scenario_revision FOR SHARE")
        db.execute("SELECT id FROM scenario FOR SHARE")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with db.transaction():
                db.execute("UPDATE scenario_revision SET operations='[]' WHERE false")


@pytest.mark.security
def test_actual_api_role_creates_forecast_but_cannot_rewrite_revision(role_urls):
    from datetime import date

    engine = create_engine(
        role_urls["api"].replace("postgresql://", "postgresql+psycopg://"), hide_parameters=True
    )
    with engine.connect() as connection, connection.begin() as transaction:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as db:
            scenario = Scenario(
                id=uid(),
                title="Runtime privilege proof",
                owner_id="hr",
                reviewer_id="finance",
                base_snapshot_id=CURRENT_SNAPSHOT,
                policy_id=POLICY_ID,
                fx_set_id=FX_BASE_ID,
                year=2028,
                planning_date=date(2028, 1, 1),
                scope_orgs=["D02"],
                rules=PlanningRules().document(),
                budget_minor="99999999999999999999",
            )
            db.add(scenario)
            db.flush()
            revision = save_revision(db, scenario, [], "hr", "Verify runtime row locks", uid())
            assert (
                db.scalar(
                    text("SELECT count(*) FROM scenario_forecast WHERE revision_id=:id"),
                    {"id": revision.id},
                )
                == 1
            )
            with pytest.raises(IntegrityError, match="immutable"):
                with db.begin_nested():
                    db.execute(
                        text("UPDATE scenario_revision SET id=id WHERE id=:id"), {"id": revision.id}
                    )
            assert db.get(Scenario, scenario.id).current_revision == 1
        transaction.rollback()
    engine.dispose()


def test_reapplying_policy_removes_accidental_column_acl(role_urls):
    admin = (
        make_url(isolated_url()).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    with psycopg.connect(admin) as db:
        db.execute("GRANT UPDATE (payload) ON outbox_event TO meridian_app")
    passwords = json.loads(Path(".local/runtime-database-credentials.json").read_text())
    provision(isolated_url(), passwords)
    with psycopg.connect(role_urls["api"]) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("UPDATE outbox_event SET payload='{}' WHERE false")
