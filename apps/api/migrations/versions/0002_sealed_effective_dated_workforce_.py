"""sealed effective dated workforce snapshots

Revision: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "authz_policy_version",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "employee",
        sa.Column("id", sa.String(length=10), nullable=False),
        sa.CheckConstraint("id ~ '^SYN-[0-9]{6}$'", name="employee_synthetic_id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "org_unit",
        sa.Column("id", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("cost_center", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "position",
        sa.Column("id", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "principal_employee",
        sa.Column("principal_id", sa.String(length=80), nullable=False),
        sa.Column("employee_id", sa.String(length=10), nullable=False),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principal.id"],
        ),
        sa.PrimaryKeyConstraint("principal_id"),
        sa.UniqueConstraint("employee_id"),
    )
    op.create_table(
        "source_batch",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("watermark", sa.Date(), nullable=False),
        sa.Column("actor_id", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["principal.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_type", "sha256"),
    )
    op.create_index(op.f("ix_source_batch_actor_id"), "source_batch", ["actor_id"], unique=False)
    op.create_table(
        "snapshot",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_batch_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("source_watermark", sa.Date(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_batch_id"],
            ["source_batch.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_batch_id"),
    )
    op.create_index(
        op.f("ix_snapshot_source_watermark"), "snapshot", ["source_watermark"], unique=False
    )
    op.create_table(
        "employee_version",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("employee_id", sa.String(length=10), nullable=False),
        sa.Column("position_id", sa.String(length=20), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("org_id", sa.String(length=10), nullable=False),
        sa.Column("manager_id", sa.String(length=10), nullable=True),
        sa.Column("job_family", sa.String(length=50), nullable=False),
        sa.Column("grade", sa.String(length=10), nullable=False),
        sa.Column("region", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("annual_pay_minor", sa.Numeric(precision=20, scale=0), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("fte", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_batch_id", sa.String(length=36), nullable=False),
        postgresql.ExcludeConstraint(
            (sa.column("snapshot_id"), "="),
            (sa.column("employee_id"), "="),
            (sa.text("daterange(valid_from,valid_to,'[)')"), "&&"),
            using="gist",
            name="employee_version_no_overlap",
        ),
        sa.CheckConstraint("currency IN ('USD','EUR','GBP','JPY','KWD')", name="employee_currency"),
        sa.CheckConstraint("annual_pay_minor >= 0", name="employee_nonnegative_pay"),
        sa.CheckConstraint("fte > 0 AND fte <= 1", name="employee_fte"),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="employee_valid_interval"
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
        ),
        sa.ForeignKeyConstraint(
            ["manager_id"],
            ["employee.id"],
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["org_unit.id"],
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["position.id"],
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshot.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_batch_id"],
            ["source_batch.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id", "employee_id", "valid_from"),
    )
    op.create_index(
        op.f("ix_employee_version_employee_id"), "employee_version", ["employee_id"], unique=False
    )
    op.create_index(
        op.f("ix_employee_version_manager_id"), "employee_version", ["manager_id"], unique=False
    )
    op.create_index(
        op.f("ix_employee_version_org_id"), "employee_version", ["org_id"], unique=False
    )
    op.create_index(
        op.f("ix_employee_version_position_id"), "employee_version", ["position_id"], unique=False
    )
    op.create_index(
        op.f("ix_employee_version_snapshot_id"), "employee_version", ["snapshot_id"], unique=False
    )
    op.create_index(
        op.f("ix_employee_version_source_batch_id"),
        "employee_version",
        ["source_batch_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_employee_version_valid_from"), "employee_version", ["valid_from"], unique=False
    )
    op.create_table(
        "org_version",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=10), nullable=False),
        sa.Column("parent_id", sa.String(length=10), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("region", sa.String(length=30), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        postgresql.ExcludeConstraint(
            (sa.column("snapshot_id"), "="),
            (sa.column("org_id"), "="),
            (sa.text("daterange(valid_from,valid_to,'[)')"), "&&"),
            using="gist",
            name="org_version_no_overlap",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["org_unit.id"],
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["org_unit.id"],
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshot.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_org_version_org_id"), "org_version", ["org_id"], unique=False)
    op.create_index(op.f("ix_org_version_parent_id"), "org_version", ["parent_id"], unique=False)
    op.create_index(
        op.f("ix_org_version_snapshot_id"), "org_version", ["snapshot_id"], unique=False
    )
    op.execute("""
        CREATE FUNCTION enforce_snapshot_seal() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'BUILDING' THEN
                    RAISE EXCEPTION 'new snapshot must be built before publication' USING ERRCODE='23514';
                END IF;
            ELSIF OLD.status = 'PUBLISHED' THEN
                RAISE EXCEPTION 'published snapshot is immutable' USING ERRCODE='23514';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            IF NEW.status NOT IN ('BUILDING', 'PUBLISHED') THEN
                RAISE EXCEPTION 'unknown snapshot status' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$;
        CREATE TRIGGER snapshot_seal BEFORE INSERT OR UPDATE OR DELETE ON snapshot
            FOR EACH ROW EXECUTE FUNCTION enforce_snapshot_seal();
        CREATE FUNCTION enforce_open_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent_status text;
        BEGIN
            SELECT status INTO parent_status FROM snapshot WHERE id = NEW.snapshot_id FOR SHARE;
            IF parent_status IS DISTINCT FROM 'BUILDING' THEN
                RAISE EXCEPTION 'version inserts require an open snapshot' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$;
    """)
    for table in ["employee_version", "org_version"]:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_immutable_change()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_open BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION enforce_open_snapshot()"
        )
    op.execute(
        "CREATE TRIGGER authz_policy_immutable BEFORE UPDATE OR DELETE ON authz_policy_version FOR EACH ROW EXECUTE FUNCTION reject_immutable_change()"
    )


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_org_version_snapshot_id"), table_name="org_version")
    op.drop_index(op.f("ix_org_version_parent_id"), table_name="org_version")
    op.drop_index(op.f("ix_org_version_org_id"), table_name="org_version")
    op.drop_table("org_version")
    op.drop_index(op.f("ix_employee_version_valid_from"), table_name="employee_version")
    op.drop_index(op.f("ix_employee_version_source_batch_id"), table_name="employee_version")
    op.drop_index(op.f("ix_employee_version_snapshot_id"), table_name="employee_version")
    op.drop_index(op.f("ix_employee_version_position_id"), table_name="employee_version")
    op.drop_index(op.f("ix_employee_version_org_id"), table_name="employee_version")
    op.drop_index(op.f("ix_employee_version_manager_id"), table_name="employee_version")
    op.drop_index(op.f("ix_employee_version_employee_id"), table_name="employee_version")
    op.drop_table("employee_version")
    op.drop_index(op.f("ix_snapshot_source_watermark"), table_name="snapshot")
    op.drop_table("snapshot")
    op.drop_index(op.f("ix_source_batch_actor_id"), table_name="source_batch")
    op.drop_table("source_batch")
    op.drop_table("principal_employee")
    op.drop_table("position")
    op.drop_table("org_unit")
    op.drop_table("employee")
    op.drop_table("authz_policy_version")
    op.execute("DROP FUNCTION enforce_open_snapshot()")
    op.execute("DROP FUNCTION enforce_snapshot_seal()")
