import { readFileSync } from "node:fs";
import { resolve } from "node:path";

function parsedUrl(value) {
  try {
    return new URL(value);
  } catch {
    throw new Error("Identity URL configuration is invalid");
  }
}

function parsedKeys(value) {
  try {
    return JSON.parse(value);
  } catch {
    throw new Error("Identity key configuration is invalid");
  }
}

function exactOrigin(value) {
  const url = parsedUrl(value);
  if (url.origin !== value || url.username || url.password)
    throw new Error("Identity origin must be exact");
  return url;
}

export function identityConfig(env = process.env) {
  const local = ["local", "test"].includes(env.APP_ENV);
  const issuer = exactOrigin(env.OIDC_ISSUER);
  const redirect = parsedUrl(env.OIDC_REDIRECT_URI);
  const database = parsedUrl(env.OIDC_DATABASE_URL);
  if (
    !env.APP_ENV ||
    !env.OIDC_CLIENT_ID ||
    !["postgres:", "postgresql:"].includes(database.protocol) ||
    redirect.pathname !== "/auth/callback" ||
    redirect.search ||
    redirect.hash ||
    redirect.username ||
    redirect.password
  )
    throw new Error("Identity configuration is incomplete");
  if (!local) {
    const web = exactOrigin(env.WEB_ORIGIN);
    if (
      env.COOKIE_SECURE !== "true" ||
      issuer.protocol !== "https:" ||
      redirect.protocol !== "https:" ||
      web.protocol !== "https:" ||
      redirect.origin !== web.origin ||
      ["localhost", "127.0.0.1", "::1"].includes(issuer.hostname) ||
      database.username.split(".")[0] !== "meridian_identity" ||
      database.searchParams.get("sslmode") !== "verify-full" ||
      !env.OIDC_JWKS_JSON ||
      !env.OIDC_COOKIE_KEYS_JSON
    )
      throw new Error(
        "Hosted identity requires exact HTTPS origins, TLS, dedicated credentials and persistent keys",
      );
  }
  const port = Number(env.PORT || issuer.port || (local ? 4017 : 10000));
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535)
    throw new Error("Invalid identity listening port");
  return {
    local,
    issuer: issuer.origin,
    host: issuer.host,
    clientId: env.OIDC_CLIENT_ID,
    redirectUri: redirect.href,
    databaseUrl: env.OIDC_DATABASE_URL,
    port,
    bindHost: env.BIND_HOST || (local ? "127.0.0.1" : "0.0.0.0"),
    dataDir: resolve(env.LOCAL_DATA_DIR || "../../.local"),
  };
}

export function identityKeys(config, env = process.env) {
  const jwks = parsedKeys(
    env.OIDC_JWKS_JSON ||
      readFileSync(resolve(config.dataDir, "oidc-keys.json"), "utf8"),
  );
  const cookies = parsedKeys(
    env.OIDC_COOKIE_KEYS_JSON ||
      readFileSync(resolve(config.dataDir, "oidc-cookie-keys.json"), "utf8"),
  );
  if (
    !Array.isArray(jwks.keys) ||
    jwks.keys.length !== 1 ||
    jwks.keys[0].kty !== "RSA" ||
    jwks.keys[0].alg !== "RS256" ||
    !jwks.keys[0].kid ||
    !jwks.keys[0].d ||
    !jwks.keys[0].n ||
    Buffer.from(jwks.keys[0].n, "base64url").length < 256 ||
    !Array.isArray(cookies) ||
    !cookies.length ||
    cookies.some(
      (key) => typeof key !== "string" || !/^[a-f0-9]{64}$/.test(key),
    )
  )
    throw new Error("Identity signing and cookie keys are invalid");
  return { jwks, cookies };
}
