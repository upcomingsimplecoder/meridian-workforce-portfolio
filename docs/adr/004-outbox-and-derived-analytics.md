# At-least-once delivery and derived analytics

Status: implemented with real PostgreSQL workers and DuckDB/dbt verification.

Business changes, audit records, and minimal outbox events commit together.
Independent consumers claim events with FOR UPDATE SKIP LOCKED. A unique
consumer receipt provides idempotency; duplicate delivery cannot create another
approval, export, or publication.

Content-addressed writes happen before SQL acknowledgement. A crash can leave an
unreferenced object, but retry verifies identical bytes and commits one artifact
and receipt. Corruption blocks acknowledgement. Retry backs off and eventually
becomes a visible dead job that requires an authorized retry with a reason.

PostgreSQL remains transactional truth. Each analytics request freezes source
versions and private Parquet inputs. The analytics consumer runs actual dbt SQL
in a private DuckDB catalog, checks counts, monetary sums, and semantic hashes,
then publishes only a nonempty, fully reconciled result.

The consumer holds one bounded transaction through the build, with a 90-second
subprocess limit. This trades connection time for a simple atomic result/receipt
boundary. Worker heartbeats commit separately so a build cannot lock out other
workers. Each run has its own catalog; there is no shared writable DuckDB file.

A dropped-event drill derives an intentionally incomplete projection without
changing source decisions. Repair uses the same pinned inputs and models,
delivers the missing event, and must recover the original numerical report hash.
Prior drift results remain visible. Snowflake is optional code, not a hosted
deployment dependency or a verified live integration.
