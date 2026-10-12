# Security

Meridian is a portfolio application for fictional data. Do not connect real
employee records, proprietary employer material, or payroll data. It has no
Morgan Stanley affiliation or compliance/enterprise MFA claim. AI is OFF.

The browser calls Next.js and FastAPI, never Meridian business tables directly.
OIDC uses generated, distinct fictional accounts. The API resolves current
database grants and checks object, action, and field scope. Runtime SQL roles
are non-owner accounts. Only operator tasks may migrate, seed, or alter grants.

Artifacts are private. Downloads require a live session, current scope,
principal-bound expiring link, and verified bytes. Hosted configuration rejects
public or unbounded buckets. Storage and signing keys belong in managed server
secrets and must never use a `NEXT_PUBLIC_*` variable.

Do not commit `.env`, `.local`, passwords, database URLs, provider keys, browser
storage state, raw traces, private logs, or unredacted failure reports. CI checks
dependencies and Git history. Public exports use an allowlist; forensic history
stays private.

Report vulnerabilities through **Security → Report a vulnerability** in GitHub.
If the private form is unavailable, request a private reporting channel in an
issue without posting sensitive details or a working exploit. Maintainers should
reproduce with fictional data, preserve private evidence, patch, and rerun gates.

Authorization bypass, role escalation, self-approval, locked edits, financial
drift, and private artifact exposure block release. Hosted execution remains
UNVERIFIED until recorded acceptance and recovery run against real services.
