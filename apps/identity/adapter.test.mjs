import { test, after } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import pg from "pg";
import { adapterFor } from "./adapter.mjs";
const url = new URL(process.env.OIDC_DATABASE_URL);
if (!["127.0.0.1", "localhost", "postgres"].includes(url.hostname))
  throw new Error("Tests require local PostgreSQL");
url.pathname = "/meridian_test";
const pool = new pg.Pool({ connectionString: url.toString(), max: 3 });
after(() => pool.end());
const Adapter = adapterFor(pool);
const adapter = new Adapter("AuthorizationCode");
test("concurrent authorization code delivery has exactly one consumer", async () => {
  const id = randomUUID();
  try {
    await adapter.upsert(id, { accountId: "synthetic:hr" }, 60);
    const attempts = await Promise.allSettled([
      adapter.consume(id),
      adapter.consume(id),
    ]);
    assert.equal(
      attempts.filter((result) => result.status === "fulfilled").length,
      1,
    );
    assert.equal(
      attempts.filter((result) => result.status === "rejected").length,
      1,
    );
    assert.equal(typeof (await adapter.find(id)).consumed, "number");
    await assert.rejects(() => adapter.consume(id));
  } finally {
    await adapter.destroy(id);
  }
});
test("expired and absent records cannot be consumed", async () => {
  const id = randomUUID();
  try {
    await adapter.upsert(id, { accountId: "synthetic:hr" }, -1);
    assert.equal(await adapter.find(id), undefined);
    await assert.rejects(() => adapter.consume(id));
    await assert.rejects(() => adapter.consume(randomUUID()));
  } finally {
    await adapter.destroy(id);
  }
});
test("revoking a grant removes its tokens without removing another grant", async () => {
  const id = randomUUID(),
    other = randomUUID(),
    grantId = randomUUID();
  try {
    await adapter.upsert(id, { grantId }, 60);
    await adapter.upsert(other, { grantId: randomUUID() }, 60);
    await adapter.revokeByGrantId(grantId);
    assert.equal(await adapter.find(id), undefined);
    await assert.rejects(() => adapter.consume(id));
    assert.ok(await adapter.find(other));
  } finally {
    await adapter.destroy(id);
    await adapter.destroy(other);
  }
});
