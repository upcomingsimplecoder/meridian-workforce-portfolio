import { test } from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { identityConfig, identityKeys } from "./config.mjs";

const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const jwk = {
  ...privateKey.export({ format: "jwk" }),
  kid: "test",
  alg: "RS256",
  use: "sig",
};
const env = {
  APP_ENV: "production",
  OIDC_ISSUER: "https://identity.example.com",
  OIDC_CLIENT_ID: "meridian-web",
  OIDC_REDIRECT_URI: "https://web.example.com/auth/callback",
  WEB_ORIGIN: "https://web.example.com",
  COOKIE_SECURE: "true",
  OIDC_DATABASE_URL:
    "postgresql://meridian_identity:private-test-password@db.example.com/postgres?sslmode=verify-full",
  OIDC_JWKS_JSON: JSON.stringify({ keys: [jwk] }),
  OIDC_COOKIE_KEYS_JSON: JSON.stringify(["ab".repeat(32)]),
};

test("hosted identity uses the exact issuer, secure redirects and persistent keys", () => {
  const config = identityConfig(env);
  assert.equal(config.issuer, env.OIDC_ISSUER);
  assert.equal(config.redirectUri, env.OIDC_REDIRECT_URI);
  assert.equal(config.port, 10000);
  assert.equal(config.bindHost, "0.0.0.0");
  assert.equal(identityKeys(config, env).jwks.keys[0].kid, "test");
});

for (const [index, change] of [
  { OIDC_ISSUER: "http://identity.example.com" },
  { OIDC_ISSUER: "https://identity.example.com/" },
  { OIDC_ISSUER: "https://127.0.0.1" },
  { OIDC_REDIRECT_URI: "http://web.example.com/auth/callback" },
  { OIDC_REDIRECT_URI: "https://attacker.example.com/auth/callback" },
  {
    OIDC_REDIRECT_URI:
      "https://web.example.com/auth/callback?returnTo=attacker",
  },
  { COOKIE_SECURE: "false" },
  {
    OIDC_DATABASE_URL: env.OIDC_DATABASE_URL.replace(
      "meridian_identity",
      "postgres",
    ),
  },
  {
    OIDC_DATABASE_URL: env.OIDC_DATABASE_URL.replace("verify-full", "require"),
  },
  { OIDC_JWKS_JSON: "" },
  { OIDC_COOKIE_KEYS_JSON: "" },
  { PORT: "0" },
].entries())
  test("hosted identity refuses unsafe configuration " + index, () => {
    assert.throws(() => identityConfig({ ...env, ...change }));
  });

test("configuration failures never echo a connection password or private key", () => {
  try {
    identityConfig({ ...env, OIDC_DATABASE_URL: "private-test-password" });
  } catch (error) {
    assert.ok(!String(error).includes("private-test-password"));
  }
  try {
    identityKeys(identityConfig(env), {
      ...env,
      OIDC_JWKS_JSON: "private-test-password",
    });
  } catch (error) {
    assert.ok(!String(error).includes("private-test-password"));
  }
});

test("hosted identity refuses public-only signing keys and short cookie secrets", () => {
  const { d, ...publicKey } = jwk;
  assert.ok(d);
  assert.throws(() =>
    identityKeys(identityConfig(env), {
      ...env,
      OIDC_JWKS_JSON: JSON.stringify({ keys: [publicKey] }),
    }),
  );
  assert.throws(() =>
    identityKeys(identityConfig(env), {
      ...env,
      OIDC_COOKIE_KEYS_JSON: JSON.stringify(["weak"]),
    }),
  );
});
