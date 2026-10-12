"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  ArrowUpRight,
  Check,
  Circle,
  Database,
  Fingerprint,
  GitBranch,
  ShieldCheck,
  Users,
} from "lucide-react";
import { getOverview, type Me } from "@/lib/api";

export function OverviewContent({ me }: { me: Me }) {
  const query = useQuery({
    queryKey: ["overview", me.id],
    queryFn: getOverview,
    refetchInterval: 15000,
  });
  if (query.isPending)
    return (
      <div className="panel loading-panel" role="status">
        Loading your workspace from PostgreSQL…
      </div>
    );
  if (query.error)
    return (
      <div className="error-box" role="alert">
        <h2>Workspace data is unavailable</h2>
        <p>The source database could not be reached.</p>
        <button className="button" onClick={() => query.refetch()}>
          Retry connection
        </button>
      </div>
    );
  const data = query.data;
  const workerHealthy =
    data.worker_last_seen &&
    new Date(data.as_of).getTime() - new Date(data.worker_last_seen).getTime() <
      60000;
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">WORKFORCE & PLANNING</div>
          <h1>
            Workspace overview<span className="heading-dot">.</span>
          </h1>
          <p>Your workforce, plans, and recent decisions.</p>
        </div>
        <span className="status-pill">
          <span className="status-dot" /> Connected to source
        </span>
      </div>
      <div className="scope-strip">
        <ShieldCheck size={16} />
        <span>
          Signed in as <strong>{me.display_name}</strong>
        </span>
        <span className="scope-divider" />
        <span>
          Access:{" "}
          {me.grants
            .map((g) => (g.scope === "*" ? "Organization-wide" : g.scope))
            .join(", ")}
        </span>
        <span className="strip-end">Policy {me.policy_version}</span>
      </div>
      <section className="metric-grid" aria-label="Live workspace status">
        <Metric
          label="Demo identities"
          value={String(data.demo_accounts).padStart(2, "0")}
          note="Separate authenticated accounts"
          icon={Users}
        />
        <Metric
          label="Source of truth"
          value="Connected"
          note="PostgreSQL · live query"
          icon={Database}
        />
        <Metric
          label="Worker status"
          value={workerHealthy ? "Running" : "Stale"}
          note={
            workerHealthy
              ? `${data.processed_events} events acknowledged`
              : "No recent worker heartbeat"
          }
          icon={GitBranch}
        />
        <Metric
          label="Pending events"
          value={String(data.pending_events).padStart(2, "0")}
          note="Durable transactional outbox"
          icon={Fingerprint}
        />
      </section>
      <div className="overview-grid">
        <section className="panel access-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">YOUR ACCESS</span>
              <h2>Signed in to Meridian.</h2>
            </div>
            <div className="panel-symbol">
              <ShieldCheck size={23} />
            </div>
          </div>
          <p className="section-copy">
            Your account determines which people, plans, and review actions you
            can access. Saved changes and approvals are retained in PostgreSQL.
          </p>
          <div className="readiness-list">
            <div>
              <span className="check-circle">
                <Check size={14} />
              </span>
              <span>
                <strong>Verified identity</strong>
                <small>OIDC login with server-assigned access</small>
              </span>
              <span className="text-badge">CONNECTED</span>
            </div>
            <div>
              <span className="check-circle">
                <Check size={14} />
              </span>
              <span>
                <strong>Durable application state</strong>
                <small>Versioned migrations and transactional audit</small>
              </span>
              <span className="text-badge">CONNECTED</span>
            </div>
            <div>
              <span className="check-circle">
                <Check size={14} />
              </span>
              <span>
                <strong>Fictional demo data</strong>
                <small>No personal records. No employer affiliation.</small>
              </span>
              <span className="text-badge">SYNTHETIC</span>
            </div>
          </div>
          <Link href="/settings" className="text-link">
            Inspect your access <ArrowRight size={16} />
          </Link>
        </section>
        <section className="panel provenance-panel">
          <span className="eyebrow light">DECISION REPLAY</span>
          <h2>
            Trace a result.
            <br />
            Reproduce it exactly.
          </h2>
          <p>
            Follow a decision from its source snapshot through assumptions,
            calculations, and human approvals.
          </p>
          <div className="provenance-chain">
            <div>
              <Database size={18} />
              <span>Source</span>
            </div>
            <span />
            <div>
              <GitBranch size={18} />
              <span>Decision</span>
            </div>
            <span />
            <div>
              <ShieldCheck size={18} />
              <span>Evidence</span>
            </div>
          </div>
          <div className="roadmap-note">
            <Circle size={9} /> Decision Replay is available in Audit
          </div>
        </section>
      </div>
      <section className="panel roadmap">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">WORKSPACES</span>
            <h2>Planning and review</h2>
          </div>
          <span className="subtle-label">
            Workforce, scenarios, compensation and talent available
          </span>
        </div>
        <div className="module-grid">
          {[
            {
              title: "Workforce Explorer",
              description: "Effective-dated people and scoped history.",
              href: "/workforce",
            },
            {
              title: "Data Quality",
              description: "Validate, quarantine, and publish sources.",
              href: "/data-quality",
            },
            {
              title: "Scenario Lab",
              description: "Compare plans with deterministic costs.",
              href: "/scenarios",
            },
            {
              title: "Compensation Studio",
              description: "Propose, review, approve, and lock.",
              href: "/compensation",
            },
            {
              title: "Talent Review",
              description:
                "Human narratives, calibration and signed revisions.",
              href: "/talent",
            },
          ].map((m) => (
            <Link className="module-card" href={m.href} key={m.href}>
              <span className="module-phase">
                <span>AVAILABLE</span>
              </span>
              <h3>
                {m.title} <ArrowUpRight size={16} />
              </h3>
              <p>{m.description}</p>
            </Link>
          ))}
        </div>
      </section>
      <div className="data-source">
        <Database size={13} /> Live source: {data.source}
        <span>
          Queried {new Date(data.as_of).toLocaleTimeString("en-US")} ·{" "}
          {data.seed_version}
        </span>
      </div>
    </>
  );
}

function Metric({
  label,
  value,
  note,
  icon: Icon,
}: {
  label: string;
  value: string;
  note: string;
  icon: typeof Users;
}) {
  return (
    <div className="metric">
      <div className="metric-top">
        <span>{label}</span>
        <Icon size={16} />
      </div>
      <strong>{value}</strong>
      <p>{note}</p>
    </div>
  );
}
