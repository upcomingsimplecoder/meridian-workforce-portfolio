"""Real PostgreSQL ACL/RLS tests in a fresh isolated database, not a cloud claim."""

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from scripts.operator_db import admin_url
from scripts.provision_db import KNOWN_TABLES, ROLE_NAMES, provision


@pytest.fixture(scope="module")
def isolated_supabase():
    operator = make_url(admin_url()).set(drivername="postgresql")
    if operator.host not in {"127.0.0.1", "localhost", "postgres"}:
        raise RuntimeError("Only native isolated privilege tests are allowed")
    name = "meridian_supabase_" + uuid4().hex[:16]
    root = Path(__file__).resolve().parents[3]
    passwords = json.loads((root / ".local/runtime-database-credentials.json").read_text())
    admin = operator.set(database=name).render_as_string(hide_password=False)
    control_url = operator.set(database="postgres").render_as_string(hide_password=False)
    created = []
    with psycopg.connect(control_url, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        for role in ("anon", "authenticated", "service_role"):
            if not control.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                control.execute(
                    sql.SQL("CREATE ROLE {} NOLOGIN {}").format(
                        sql.Identifier(role),
                        sql.SQL("BYPASSRLS" if role == "service_role" else "NOBYPASSRLS"),
                    )
                )
                created.append(role)
    try:
        subprocess.run(
            [str(root / ".venv/bin/alembic"), "upgrade", "head"],
            cwd=root,
            env=os.environ | {"DATABASE_URL": admin, "APP_ENV": "test"},
            check=True,
            capture_output=True,
        )
        with psycopg.connect(admin) as db:
            # Reproduce broad platform ACLs; hardening must remove them.
            db.execute("GRANT USAGE ON SCHEMA public TO anon,authenticated,service_role")
            db.execute(
                "GRANT ALL ON ALL TABLES IN SCHEMA public TO anon,authenticated,service_role"
            )
            db.execute(
                "GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO anon,authenticated,service_role"
            )
            db.execute(
                "INSERT INTO principal(id,oidc_sub,display_name,active) VALUES ('probe','synthetic:probe','Fictional privilege test',true)"
            )
        provision(admin, passwords, supabase=True)
        yield admin, passwords
    finally:
        with psycopg.connect(control_url, autocommit=True) as control:
            control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            for role in created:
                control.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.mark.security
@pytest.mark.parametrize("role", ["anon", "authenticated", "service_role"])
def test_supabase_browser_and_service_roles_cannot_read_or_mutate_business_tables(
    isolated_supabase, role
):
    admin, _ = isolated_supabase
    with psycopg.connect(admin) as db:
        db.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        for statement in (
            "SELECT * FROM public.principal",
            "SELECT annual_pay_minor FROM public.employee_version",
            "UPDATE public.role_assignment SET scope='*' WHERE false",
            "SELECT public.reject_immutable_change()",
            "CREATE TABLE public.browser_escape(id int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with db.transaction():
                    db.execute(statement)
        db.execute("RESET ROLE")


@pytest.mark.security
def test_every_business_table_has_only_trusted_sql_rls_policy(isolated_supabase):
    admin, _ = isolated_supabase
    with psycopg.connect(admin) as db:
        tables = db.execute(
            "SELECT relname,relrowsecurity FROM pg_class JOIN pg_namespace ON relnamespace=pg_namespace.oid WHERE nspname='public' AND relkind='r'"
        ).fetchall()
        assert {name for name, _ in tables} == KNOWN_TABLES
        assert all(enabled for _, enabled in tables)
        policies = db.execute(
            "SELECT policyname,roles FROM pg_policies WHERE schemaname='public'"
        ).fetchall()
        assert len(policies) == len(KNOWN_TABLES)
        assert all(
            name == "meridian_trusted_sql" and set(roles) == set(ROLE_NAMES.values())
            for name, roles in policies
        )


@pytest.mark.security
@pytest.mark.parametrize("kind", ["api", "worker", "identity"])
def test_trusted_runtime_role_can_read_only_its_existing_acl_inventory(isolated_supabase, kind):
    admin, passwords = isolated_supabase
    role = ROLE_NAMES[kind]
    url = (
        make_url(admin)
        .set(username=role, password=passwords[role])
        .render_as_string(hide_password=False)
    )
    with psycopg.connect(url) as db:
        assert (
            db.execute("SELECT oidc_sub FROM public.principal").fetchone()[0] == "synthetic:probe"
        )
        assert (
            db.execute(
                "SELECT count(*) FROM public.oidc_record"
                if kind == "identity"
                else "SELECT count(*) FROM public.employee_version"
            ).fetchone()[0]
            == 0
        )
        denied = (
            "SELECT * FROM public.employee_version"
            if kind == "identity"
            else "SELECT * FROM public.demo_credential"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with db.transaction():
                db.execute(denied)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with db.transaction():
                db.execute("UPDATE public.role_assignment SET scope='*' WHERE false")


@pytest.mark.security
def test_reapplying_supabase_grants_removes_new_browser_acl(isolated_supabase):
    admin, passwords = isolated_supabase
    with psycopg.connect(admin) as db:
        db.execute("GRANT SELECT ON public.principal TO authenticated")
    provision(admin, passwords, supabase=True)
    with psycopg.connect(admin) as db:
        assert (
            db.execute(
                "SELECT has_table_privilege('authenticated','public.principal','SELECT')"
            ).fetchone()[0]
            is False
        )


@pytest.mark.security
def test_unexpected_permissive_policy_refuses_provisioning_atomically(isolated_supabase):
    admin, passwords = isolated_supabase
    with psycopg.connect(admin) as db:
        db.execute(
            "CREATE POLICY unexpected_browser ON public.principal TO authenticated USING (true)"
        )
    try:
        with pytest.raises(RuntimeError, match="Unexpected business-table policy"):
            provision(admin, passwords, supabase=True)
    finally:
        with psycopg.connect(admin) as db:
            db.execute("DROP POLICY unexpected_browser ON public.principal")
