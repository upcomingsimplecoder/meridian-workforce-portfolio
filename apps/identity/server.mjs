import Provider from "oidc-provider";
import { adapterFor } from "./adapter.mjs";
import { identityConfig, identityKeys } from "./config.mjs";
import express from "express";
import pg from "pg";
import { randomBytes, scryptSync, timingSafeEqual } from "node:crypto";
const config = identityConfig();
const keys = identityKeys(config);
const issuer = config.issuer;
const pool = new pg.Pool({
  connectionString: config.databaseUrl,
  max: 4,
  connectionTimeoutMillis: 5000,
  query_timeout: 10000,
});
// Idle PostgreSQL connections emit errors on a server restart. Handle that
// event so pg can discard the connection without crashing or dumping secrets.
pool.on("error", () => console.error("identity_database_unavailable"));
const Adapter = adapterFor(pool);

const provider = new Provider(issuer, {
  adapter: Adapter,
  clients: [
    {
      client_id: config.clientId,
      redirect_uris: [config.redirectUri],
      response_types: ["code"],
      grant_types: ["authorization_code"],
      token_endpoint_auth_method: "none",
    },
  ],
  jwks: keys.jwks,
  cookies: {
    keys: keys.cookies,
    long: { sameSite: "lax", secure: !config.local, httpOnly: true },
    short: { sameSite: "lax", secure: !config.local, httpOnly: true },
  },
  features: { devInteractions: { enabled: false } },
  claims: { openid: ["sub"] },
  pkce: { required: () => true },
  ttl: {
    AccessToken: 600,
    AuthorizationCode: 60,
    IdToken: 600,
    Session: 3600,
    Interaction: 300,
    Grant: 3600,
  },
  interactions: {
    url: (_ctx, interaction) => `/interaction/${interaction.uid}`,
  },
  findAccount: async (_ctx, sub) => {
    if (!sub.startsWith("synthetic:")) return undefined;
    const { rows } = await pool.query(
      "SELECT oidc_sub FROM principal WHERE oidc_sub=$1 AND active=true",
      [sub],
    );
    return rows.length
      ? { accountId: sub, claims: async () => ({ sub }) }
      : undefined;
  },
});
const app = express();
app.disable("x-powered-by");
if (!config.local) {
  app.set("trust proxy", 1);
  provider.proxy = true;
}
app.get("/health", (_req, res) =>
  res.json({ status: "ok", service: "meridian-identity" }),
);
app.use((req, res, next) => {
  if (!config.local && (req.headers.host !== config.host || !req.secure))
    return res.status(400).send("Use the configured HTTPS identity service.");
  next();
});
app.use((_req, res, next) => {
  const scriptNonce = randomBytes(16).toString("base64");
  res.set({
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin",
    // The provider adds the exact SHA-256 of its session-switch continuation
    // script only when script-src is explicitly present. Other inline scripts
    // remain forbidden; no broad unsafe-inline script allowance is needed.
    "Content-Security-Policy": `default-src 'none'; script-src 'nonce-${scriptNonce}'; style-src 'unsafe-inline'; form-action 'self' ${new URL(process.env.OIDC_REDIRECT_URI).origin}; frame-ancestors 'none'; base-uri 'none'`,
  });
  if (!config.local) res.set("Strict-Transport-Security", "max-age=31536000");
  next();
});
const attempts = new Map();
function form(uid, error = "") {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · Meridian</title><style>body{margin:0;background:#f4f5f7;color:#172b46;font:16px system-ui}main{max-width:390px;margin:12vh auto;background:white;padding:42px;border:1px solid #dfe4eb;border-radius:12px}h1{font-size:30px}label{display:block;margin:20px 0 6px}input,button{box-sizing:border-box;width:100%;padding:13px;border:1px solid #8997a8;border-radius:6px;font:inherit}button{background:#182f4d;color:white;margin-top:26px;cursor:pointer}input:focus,button:focus{outline:3px solid #377d87;outline-offset:2px}p{line-height:1.6;color:#56667b}.error{color:#a32b35}.brand{letter-spacing:4px;font-size:13px;font-weight:700}</style></head><body><main><div class="brand">MERIDIAN / IDENTITY</div><h1>Sign in to Meridian.</h1><p>Use your assigned synthetic account. The server controls access.</p>${error ? '<p class="error" role="alert">Incorrect username or password.</p>' : ""}<form method="post" action="/interaction/${encodeURIComponent(uid)}/login"><label for="username">Username</label><input id="username" name="username" autocomplete="username" maxlength="80" required autofocus><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" maxlength="200" required><button type="submit">Sign in</button></form><p>Independent portfolio project. Fictional accounts only.<br>Access credentials are issued by the demo operator.</p></main></body></html>`;
}
app.get("/interaction/:uid", async (req, res) => {
  const details = await provider.interactionDetails(req, res);
  if (details.prompt.name === "consent") {
    const grant = new provider.Grant({
      accountId: details.session.accountId,
      clientId: details.params.client_id,
    });
    grant.addOIDCScope("openid");
    const grantId = await grant.save();
    return provider.interactionFinished(
      req,
      res,
      { consent: { grantId } },
      { mergeWithLastSubmission: true },
    );
  }
  if (details.prompt.name !== "login")
    return res.status(400).send("Unsupported login interaction.");
  res.type("html").send(form(details.uid));
});
// A browser refresh or copied link can land on the form's POST action. Keep
// that URL useful without changing the canonical OIDC interaction route.
app.get("/interaction/:uid/login", (req, res) => {
  res.redirect(303, `/interaction/${encodeURIComponent(req.params.uid)}`);
});
app.post(
  "/interaction/:uid/login",
  express.urlencoded({ extended: false, limit: "4kb" }),
  async (req, res) => {
    if (req.headers.origin !== issuer)
      return res.status(403).send("Invalid login origin.");
    const details = await provider.interactionDetails(req, res);
    if (details.prompt.name !== "login")
      return res.status(400).send("Invalid login step.");
    const key = req.ip;
    for (const [k, entry] of attempts)
      if (Date.now() - entry.at > 60000) attempts.delete(k);
    const entry = attempts.get(key) || { count: 0, at: Date.now() };
    entry.count++;
    attempts.set(key, entry);
    if (entry.count > 30)
      return res
        .status(429)
        .set(
          "Retry-After",
          String(
            Math.max(1, Math.ceil((60000 - Date.now() + entry.at) / 1000)),
          ),
        )
        .send("Too many login attempts. Try again in one minute.");
    const { username, password } = req.body;
    if (
      typeof username !== "string" ||
      typeof password !== "string" ||
      password.length > 200
    )
      return res.status(400).send("Invalid credentials.");
    const { rows } = await pool.query(
      "SELECT c.salt,c.password_hash,p.oidc_sub FROM demo_credential c JOIN principal p ON p.id=c.principal_id WHERE c.username=$1 AND p.active=true AND p.oidc_sub LIKE 'synthetic:%'",
      [username],
    );
    const record = rows[0];
    const salt = record?.salt || "00000000000000000000000000000000";
    const hash = scryptSync(password, Buffer.from(salt, "hex"), 64, {
      N: 16384,
      r: 8,
      p: 1,
    });
    const expected = Buffer.from(
      record?.password_hash || "00".repeat(64),
      "hex",
    );
    if (!timingSafeEqual(hash, expected) || !record)
      return res.status(401).type("html").send(form(details.uid, "invalid"));
    return provider.interactionFinished(
      req,
      res,
      { login: { accountId: record.oidc_sub } },
      { mergeWithLastSubmission: false },
    );
  },
);
app.use(provider.callback());
app.use((_err, _req, res, _next) =>
  res.status(400).send("Identity request failed. Restart sign in."),
);
app.listen(config.port, config.bindHost, () =>
  console.log("Meridian synthetic OIDC provider ready"),
);
