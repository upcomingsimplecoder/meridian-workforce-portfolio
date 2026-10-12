import { errors } from "oidc-provider";

export function adapterFor(pool) {
  return class Adapter {
    constructor(model) {
      this.model = model;
    }
    async upsert(id, payload, expiresIn) {
      await pool.query(
        "INSERT INTO oidc_record(model,id,payload,expires_at) VALUES($1,$2,$3,$4) ON CONFLICT(model,id) DO UPDATE SET payload=$3,expires_at=$4",
        [
          this.model,
          id,
          payload,
          expiresIn ? new Date(Date.now() + expiresIn * 1000) : null,
        ],
      );
    }
    async find(id) {
      const { rows } = await pool.query(
        "SELECT payload FROM oidc_record WHERE model=$1 AND id=$2 AND (expires_at IS NULL OR expires_at>now())",
        [this.model, id],
      );
      return rows[0]?.payload;
    }
    async findByUid(uid) {
      return this.findByField("uid", uid);
    }
    async findByUserCode(code) {
      return this.findByField("userCode", code);
    }
    async findByField(field, value) {
      const { rows } = await pool.query(
        "SELECT payload FROM oidc_record WHERE model=$1 AND payload->>$2=$3 AND (expires_at IS NULL OR expires_at>now()) LIMIT 1",
        [this.model, field, value],
      );
      return rows[0]?.payload;
    }
    async destroy(id) {
      await pool.query("DELETE FROM oidc_record WHERE model=$1 AND id=$2", [
        this.model,
        id,
      ]);
    }
    async consume(id) {
      const result = await pool.query(
        "UPDATE oidc_record SET payload=jsonb_set(payload,'{consumed}',to_jsonb(extract(epoch from now())::bigint)) WHERE model=$1 AND id=$2 AND NOT payload ? 'consumed' AND (expires_at IS NULL OR expires_at>now())",
        [this.model, id],
      );
      if (result.rowCount !== 1)
        throw new errors.InvalidGrant(
          "Authorization record is expired, consumed, or unavailable.",
        );
    }
    async revokeByGrantId(grantId) {
      await pool.query(
        "DELETE FROM oidc_record WHERE model=$1 AND payload->>'grantId'=$2",
        [this.model, grantId],
      );
    }
  };
}
