"use client";
import { ShieldCheck } from "lucide-react";
import { Shell } from "@/components/shell";
export default function SettingsPage() {
  return (
    <Shell>
      {(me) => (
        <>
          <div className="page-heading">
            <div>
              <div className="eyebrow">WORKSPACE & ACCESS</div>
              <h1>Your access</h1>
              <p>Your identity and permissions come from the server.</p>
            </div>
          </div>
          <section className="panel settings-panel">
            <ShieldCheck size={26} />
            <h2>{me.display_name}</h2>
            <dl>
              <dt>Principal</dt>
              <dd>{me.id}</dd>
              <dt>Policy version</dt>
              <dd>{me.policy_version}</dd>
              <dt>Session expires</dt>
              <dd>{new Date(me.expires_at).toLocaleString()}</dd>
              <dt>Role grants</dt>
              <dd>
                {me.grants.map((g) => `${g.role} / ${g.scope}`).join(", ")}
              </dd>
            </dl>
            <h3>Granted capabilities</h3>
            <div className="capabilities">
              {me.capabilities.map((c) => (
                <span key={c}>{c}</span>
              ))}
            </div>
            <p className="fine-print">
              A grant defines access; it does not imply the corresponding module
              has been implemented. Synthetic local environment. AI is off.
              External Snowflake validation and deployment are unverified.
            </p>
          </section>
        </>
      )}
    </Shell>
  );
}
