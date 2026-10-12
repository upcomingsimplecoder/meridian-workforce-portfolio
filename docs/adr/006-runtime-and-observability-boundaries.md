# Runtime credentials and bounded observability

Status: local permission and recovery checks verified; hosted acceptance pending.

API, worker, and identity processes have distinct non-owner PostgreSQL accounts.
Explicit table and column grants limit each account. API cannot mint worker
verification, workers cannot mint human approvals or sessions, and identity
cannot read business records. Operator credentials are confined to migration,
provisioning, and restore commands.

Provisioning rejects unknown tables, elevated roles, and unexpected memberships.
Reapplying it revokes abandoned table and column privileges. On Supabase, browser
roles receive no business-table or function access, while RLS permits only the
trusted SQL runtime roles. Object and field authorization still live in FastAPI.

Technical traces retain server-generated IDs, registered route templates,
allowlisted event types, duration, outcome, and status. They exclude payloads,
personnel narratives, cookies, SQL parameters, and arbitrary incoming trace
attributes. Trace IDs grant no access. Outbox trace records link API mutations
to consumer attempts without inventing missing historical parents.

Operations reads are bounded and label missing or stale observations. These
measurements do not establish a production SLO. Local recovery encrypts a
database/object/key inventory, restores it into an isolated database, then checks
counts and exact financial replay before reapplying runtime grants. It does not
establish cloud PITR, offsite durability, or a hosted recovery objective.
