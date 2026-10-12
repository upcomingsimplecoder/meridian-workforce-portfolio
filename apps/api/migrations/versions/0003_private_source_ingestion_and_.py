"""private source ingestion and publication manifests

Revision: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "audit_event", sa.Column("entity_version", sa.Integer(), nullable=False, server_default="1")
    )
    op.create_table(
        "idempotency_receipt",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=80), nullable=False),
        sa.Column("route", sa.String(length=100), nullable=False),
        sa.Column("key", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["principal.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_id", "route", "key"),
    )
    op.create_index(
        op.f("ix_idempotency_receipt_actor_id"), "idempotency_receipt", ["actor_id"], unique=False
    )
    op.create_index("ix_idempotency_receipt_request_id", "idempotency_receipt", ["request_id"])
    op.create_table(
        "upload_intent",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=80), nullable=False),
        sa.Column("media_type", sa.String(length=30), nullable=False),
        sa.Column("declared_sha256", sa.String(length=64), nullable=False),
        sa.Column("declared_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["principal.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_upload_intent_actor_id"), "upload_intent", ["actor_id"], unique=False)
    op.create_table(
        "ingestion_error",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(length=40), nullable=False),
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("redacted_summary", sa.String(length=150), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["source_batch.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "row_number"),
    )
    op.create_index(
        op.f("ix_ingestion_error_batch_id"), "ingestion_error", ["batch_id"], unique=False
    )
    op.create_table(
        "ingestion_record",
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("canonical", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["source_batch.id"],
        ),
        sa.PrimaryKeyConstraint("batch_id", "row_number"),
    )
    op.create_table(
        "ingestion_input",
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("upload_id", sa.String(length=36), nullable=False),
        sa.Column("base_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("supersedes_batch_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=True),
        sa.Column("parser_version", sa.String(length=30), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["base_snapshot_id"],
            ["snapshot.id"],
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["source_batch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_batch_id"],
            ["source_batch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["upload_id"],
            ["upload_intent.id"],
        ),
        sa.PrimaryKeyConstraint("batch_id"),
        sa.UniqueConstraint("upload_id"),
    )
    op.create_index(
        op.f("ix_ingestion_input_base_snapshot_id"),
        "ingestion_input",
        ["base_snapshot_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ingestion_input_supersedes_batch_id"),
        "ingestion_input",
        ["supersedes_batch_id"],
        unique=False,
    )
    op.create_table(
        "ingestion_manifest",
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["source_batch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshot.id"],
        ),
        sa.PrimaryKeyConstraint("batch_id"),
        sa.UniqueConstraint("snapshot_id"),
    )
    op.create_table(
        "snapshot_publication",
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshot.id"],
        ),
        sa.PrimaryKeyConstraint("snapshot_id"),
        sa.UniqueConstraint("ordinal"),
    )
    op.execute(
        "INSERT INTO snapshot_publication(snapshot_id) SELECT id FROM snapshot WHERE status='PUBLISHED' ORDER BY captured_at,id"
    )
    for table in [
        "ingestion_record",
        "ingestion_error",
        "ingestion_manifest",
        "snapshot_publication",
        "idempotency_receipt",
    ]:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_immutable_change()"
        )
    op.execute("""
        CREATE FUNCTION enforce_open_ingestion() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent_state text;
        BEGIN
            SELECT state INTO parent_state FROM source_batch WHERE id=NEW.batch_id FOR SHARE;
            IF parent_state IS DISTINCT FROM 'RECEIVED' THEN
                RAISE EXCEPTION 'validation rows require an open source batch' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$;
        CREATE TRIGGER ingestion_record_open BEFORE INSERT ON ingestion_record FOR EACH ROW EXECUTE FUNCTION enforce_open_ingestion();
        CREATE TRIGGER ingestion_error_open BEFORE INSERT ON ingestion_error FOR EACH ROW EXECUTE FUNCTION enforce_open_ingestion();
        CREATE FUNCTION enforce_published_source() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='DELETE' OR OLD.state IN ('PUBLISHED','QUARANTINED') THEN
                RAISE EXCEPTION 'sealed source batch is immutable' USING ERRCODE='23514';
            END IF;
            IF (to_jsonb(OLD) - ARRAY['state','accepted_count','rejected_count','published_at'])
                IS DISTINCT FROM (to_jsonb(NEW) - ARRAY['state','accepted_count','rejected_count','published_at']) THEN
                RAISE EXCEPTION 'source identity and provenance are immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.state='VALIDATED' AND (NEW.state<>'PUBLISHED' OR NEW.published_at IS NULL
                OR NEW.accepted_count<>OLD.accepted_count OR NEW.rejected_count<>OLD.rejected_count) THEN
                RAISE EXCEPTION 'validated source may only be published' USING ERRCODE='23514';
            END IF;
            IF OLD.state='RECEIVED' AND NEW.state NOT IN ('VALIDATED','QUARANTINED') THEN
                RAISE EXCEPTION 'source must complete validation first' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$;
        CREATE TRIGGER source_batch_sealed BEFORE UPDATE OR DELETE ON source_batch FOR EACH ROW EXECUTE FUNCTION enforce_published_source();
        CREATE FUNCTION enforce_pinned_input() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='DELETE' THEN
                RAISE EXCEPTION 'source input is immutable' USING ERRCODE='23514';
            END IF;
            IF (to_jsonb(OLD) - ARRAY['version','total_count','validated_at'])
                IS DISTINCT FROM (to_jsonb(NEW) - ARRAY['version','total_count','validated_at'])
                OR NEW.version<>OLD.version+1 THEN
                RAISE EXCEPTION 'source input and parser pins are immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.validated_at IS NOT NULL AND (NEW.validated_at IS DISTINCT FROM OLD.validated_at
                OR NEW.total_count IS DISTINCT FROM OLD.total_count) THEN
                RAISE EXCEPTION 'source validation is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$;
        CREATE TRIGGER ingestion_input_pinned BEFORE UPDATE OR DELETE ON ingestion_input FOR EACH ROW EXECUTE FUNCTION enforce_pinned_input();
        CREATE FUNCTION enforce_upload_identity() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='DELETE' OR OLD.uploaded_at IS NOT NULL
                OR (to_jsonb(OLD) - ARRAY['storage_key','uploaded_at'])
                IS DISTINCT FROM (to_jsonb(NEW) - ARRAY['storage_key','uploaded_at']) THEN
                RAISE EXCEPTION 'upload identity is immutable' USING ERRCODE='23514';
            END IF;
            IF NEW.storage_key IS DISTINCT FROM OLD.declared_sha256 OR NEW.uploaded_at IS NULL THEN
                RAISE EXCEPTION 'upload must match declared content address' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$;
        CREATE TRIGGER upload_intent_identity BEFORE UPDATE OR DELETE ON upload_intent FOR EACH ROW EXECUTE FUNCTION enforce_upload_identity();
    """)


def downgrade():
    op.execute("DROP TRIGGER source_batch_sealed ON source_batch")
    op.execute("DROP FUNCTION enforce_published_source()")
    op.drop_table("snapshot_publication")
    op.drop_table("ingestion_manifest")
    op.drop_index(op.f("ix_ingestion_input_supersedes_batch_id"), table_name="ingestion_input")
    op.drop_index(op.f("ix_ingestion_input_base_snapshot_id"), table_name="ingestion_input")
    op.drop_table("ingestion_input")
    op.drop_table("ingestion_record")
    op.drop_index(op.f("ix_ingestion_error_batch_id"), table_name="ingestion_error")
    op.drop_table("ingestion_error")
    op.drop_index(op.f("ix_upload_intent_actor_id"), table_name="upload_intent")
    op.drop_table("upload_intent")
    op.drop_index(op.f("ix_idempotency_receipt_actor_id"), table_name="idempotency_receipt")
    op.drop_index("ix_idempotency_receipt_request_id", table_name="idempotency_receipt")
    op.drop_table("idempotency_receipt")
    op.drop_column("audit_event", "entity_version")
    op.execute("DROP FUNCTION enforce_open_ingestion()")
    op.execute("DROP FUNCTION enforce_pinned_input()")
    op.execute("DROP FUNCTION enforce_upload_identity()")
