"""Immutable technical spans and transactional API-to-worker correlation.

Revision: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "outbox_trace",
        sa.Column("event_id", sa.String(36), sa.ForeignKey("outbox_event.id"), primary_key=True),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("parent_span_id", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND parent_span_id ~ '^[0-9a-f]{16}$'",
            name="outbox_trace_ids",
        ),
    )
    op.create_index("ix_outbox_trace_trace_id", "outbox_trace", ["trace_id"])
    op.create_table(
        "trace_span",
        sa.Column("span_id", sa.String(16), primary_key=True),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("parent_span_id", sa.String(16)),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("service", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("outcome", sa.String(5), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Numeric(14, 3), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("route", sa.String(200)),
        sa.Column("event_type", sa.String(100)),
        sa.Column("event_id", sa.String(36)),
        sa.CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND span_id ~ '^[0-9a-f]{16}$'", name="trace_span_ids"
        ),
        sa.CheckConstraint(
            "service IN ('meridian-api','meridian-worker','meridian-analytics-worker')",
            name="trace_span_service",
        ),
        sa.CheckConstraint(
            "kind IN ('SERVER','CONSUMER') AND outcome IN ('OK','ERROR')",
            name="trace_span_kind_outcome",
        ),
        sa.CheckConstraint(
            "duration_ms >= 0 AND (http_status IS NULL OR http_status BETWEEN 100 AND 599)",
            name="trace_span_bounds",
        ),
    )
    for name in ("trace_id", "request_id", "started_at", "event_id"):
        op.create_index("ix_trace_span_" + name, "trace_span", [name])
    op.execute("""
        CREATE FUNCTION meridian_trace_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Technical trace metadata is append-only'; END $$;
        CREATE TRIGGER outbox_trace_immutable BEFORE UPDATE OR DELETE ON outbox_trace
            FOR EACH ROW EXECUTE FUNCTION meridian_trace_immutable();
        CREATE TRIGGER trace_span_immutable BEFORE UPDATE OR DELETE ON trace_span
            FOR EACH ROW EXECUTE FUNCTION meridian_trace_immutable();
        CREATE FUNCTION meridian_trace_runtime() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF current_user = 'meridian_app' AND NEW.service <> 'meridian-api' THEN
            RAISE EXCEPTION 'API cannot impersonate worker spans';
          ELSIF current_user = 'meridian_worker' AND NEW.service = 'meridian-api' THEN
            RAISE EXCEPTION 'Worker cannot impersonate API spans';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trace_span_runtime BEFORE INSERT ON trace_span
            FOR EACH ROW EXECUTE FUNCTION meridian_trace_runtime();
    """)


def downgrade():
    op.drop_table("trace_span")
    op.drop_table("outbox_trace")
    op.execute("DROP FUNCTION meridian_trace_runtime(); DROP FUNCTION meridian_trace_immutable()")
