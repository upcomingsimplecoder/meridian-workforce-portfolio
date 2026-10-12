import { generateKeyPair, exportJWK } from 'jose';
import { existsSync, writeFileSync, mkdirSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
const directory = process.env.LOCAL_DATA_DIR || '.local';
mkdirSync(directory, { recursive: true, mode: 0o700 });
if (!existsSync(`${directory}/oidc-keys.json`)) {
  const { privateKey } = await generateKeyPair('RS256', { extractable: true });
  const key = await exportJWK(privateKey);
  key.kid = randomBytes(12).toString('hex'); key.use = 'sig'; key.alg = 'RS256';
  writeFileSync(`${directory}/oidc-keys.json`, JSON.stringify({ keys: [key] }), { mode: 0o600 });
  writeFileSync(`${directory}/oidc-cookie-keys.json`, JSON.stringify([randomBytes(32).toString('hex')]), { mode: 0o600 });
}
