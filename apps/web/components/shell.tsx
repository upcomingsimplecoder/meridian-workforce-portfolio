"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUpRight,
  ChartNoAxesCombined,
  ChevronRight,
  CircleHelp,
  ClipboardCheck,
  Database,
  FileClock,
  LayoutDashboard,
  LogOut,
  Menu,
  Settings2,
  ShieldCheck,
  Users,
  Wallet,
  X,
  type LucideIcon,
} from "lucide-react";
import { api, ApiError, getMe, type Me } from "@/lib/api";
import { logoutSucceeded } from "@/lib/session";

type NavigationIdentity = Pick<Me, "capabilities" | "grants">;
type NavigationItem = {
  label: string;
  href: string;
  icon: LucideIcon;
  section: "workspace" | "governance";
  visible: (me: NavigationIdentity) => boolean;
};

const alwaysVisible = () => true;
const hasAny =
  (...capabilities: string[]) =>
  (me: NavigationIdentity) =>
    capabilities.some((capability) => me.capabilities.includes(capability));

const canViewCompensation = (me: NavigationIdentity) =>
  me.capabilities.includes("comp.evidence") ||
  (me.capabilities.includes("people.pay") &&
    [
      "comp.manage",
      "comp.read",
      "comp.propose",
      "comp.approve",
      "comp.hr.signoff",
      "comp.finance.signoff",
    ].some((capability) => me.capabilities.includes(capability)));

const canViewTalent = (me: NavigationIdentity) =>
  me.capabilities.includes("audit.read") ||
  (me.capabilities.includes("people.read") &&
    ["reviews.manage", "reviews.write", "reviews.approve"].some((capability) =>
      me.capabilities.includes(capability),
    ));

export const navigation: NavigationItem[] = [
  {
    label: "Overview",
    href: "/",
    icon: LayoutDashboard,
    section: "workspace",
    visible: alwaysVisible,
  },
  {
    label: "Workforce",
    href: "/workforce",
    icon: Users,
    section: "workspace",
    visible: hasAny("people.read", "analytics.read"),
  },
  {
    label: "Scenario Lab",
    href: "/scenarios",
    icon: ChartNoAxesCombined,
    section: "workspace",
    visible: hasAny("planning.write"),
  },
  {
    label: "Compensation",
    href: "/compensation",
    icon: Wallet,
    section: "workspace",
    visible: canViewCompensation,
  },
  {
    label: "Talent",
    href: "/talent",
    icon: ClipboardCheck,
    section: "workspace",
    visible: canViewTalent,
  },
  {
    label: "Analytics",
    href: "/analytics",
    icon: ChartNoAxesCombined,
    section: "workspace",
    visible: hasAny("analytics.full", "analytics.evidence", "analytics.read"),
  },
  {
    label: "Operations",
    href: "/operations",
    icon: ShieldCheck,
    section: "governance",
    visible: hasAny("operations.read"),
  },
  {
    label: "Data Quality",
    href: "/data-quality",
    icon: Database,
    section: "governance",
    visible: hasAny("ingestion.write"),
  },
  {
    label: "Audit",
    href: "/audit",
    icon: FileClock,
    section: "governance",
    visible: hasAny("replay.full", "replay.evidence"),
  },
  {
    label: "Settings",
    href: "/settings",
    icon: Settings2,
    section: "governance",
    visible: alwaysVisible,
  },
];

export function visibleNavigation(me: NavigationIdentity) {
  return navigation.filter((item) => item.visible(me));
}

export function Brand() {
  return (
    <span className="brand">
      <span className="brand-mark" aria-hidden="true">
        M<span />
      </span>
      <span>
        meridian<span className="brand-caption">WORKFORCE INTELLIGENCE</span>
      </span>
    </span>
  );
}

export function Shell({ children }: { children: (me: Me) => React.ReactNode }) {
  const pathname = usePathname();
  const [menu, setMenu] = useState(false);
  const [logoutError, setLogoutError] = useState("");
  const client = useQueryClient();
  const identity = useQuery({ queryKey: ["me"], queryFn: getMe });
  if (identity.isPending)
    return (
      <main className="center-state" aria-busy="true">
        <Brand />
        <p role="status">Connecting to your workspace…</p>
      </main>
    );
  if (identity.error) {
    const signedOut =
      identity.error instanceof ApiError && identity.error.status === 401;
    return (
      <main className="login-page">
        <div className="login-story">
          <Brand />
          <span className="eyebrow light">WORKFORCE PLANNING & REVIEW</span>
          <h1>
            People.
            <br />
            Plans.
            <br />
            <em>Provenance.</em>
          </h1>
          <p>
            Plan workforce costs, review compensation, and trace each decision
            to its inputs and approvals.
          </p>
          <div className="login-foot">
            Independent portfolio project · Synthetic data only
          </div>
        </div>
        <div className="login-action">
          <div className="eyebrow">MERIDIAN WORKSPACE</div>
          <h2>
            {signedOut
              ? "Sign in to your workspace."
              : "Connection interrupted."}
          </h2>
          <p>
            {signedOut
              ? "Sign in to your assigned workspace. Your identity, scope, and permissions are verified by the server."
              : "The workspace could not be reached. Your data has not been changed."}
          </p>
          {signedOut ? (
            <a className="button primary" href="/auth/login">
              Sign in to Meridian <ArrowUpRight size={17} />
            </a>
          ) : (
            <button
              tabIndex={0}
              className="button primary"
              onClick={() => identity.refetch()}
            >
              Try again
            </button>
          )}
          <div className="access-note">
            <ShieldCheck size={20} />
            <span>
              Protected access
              <br />
              <small>Separate accounts. Server-enforced permissions.</small>
            </span>
          </div>
          <p className="fine-print">
            Each demo account has its own generated credentials. All people and
            organizations are fictional.
          </p>
        </div>
      </main>
    );
  }
  const me = identity.data;
  async function logout() {
    setLogoutError("");
    try {
      const { response } = await api.POST("/api/v1/logout", {
        headers: { "X-CSRF-Token": me.csrf_token },
      });
      if (!logoutSucceeded(response)) {
        setLogoutError("Sign out failed. Please try again.");
        return;
      }
      client.clear();
      window.location.assign("/");
    } catch {
      setLogoutError("Sign out failed. Please try again.");
    }
  }
  const current =
    navigation.find((n) => n.href === pathname)?.label || "Workspace";
  const visibleItems = visibleNavigation(me);
  return (
    <div className="app-shell">
      <a tabIndex={0} className="skip-link" href="#main-content">
        Skip to content
      </a>
      <aside className={`sidebar ${menu ? "open" : ""}`}>
        <div className="sidebar-brand">
          <Link tabIndex={0} href="/" aria-label="Meridian overview">
            <Brand />
          </Link>
          <button
            tabIndex={0}
            className="icon-button mobile-only"
            aria-label="Close navigation"
            onClick={() => setMenu(false)}
          >
            <X size={20} />
          </button>
        </div>
        <div className="workspace-switch">
          <span className="workspace-avatar">S</span>
          <span>
            Synthetic workspace<small>PORTFOLIO DEMO</small>
          </span>
          <ChevronRight size={14} />
        </div>
        <nav aria-label="Main navigation">
          {visibleItems.map((item, i) => (
            <div key={item.href}>
              {item.section === "governance" &&
                visibleItems[i - 1]?.section !== item.section && (
                  <div className="nav-section">GOVERNANCE & SYSTEM</div>
                )}
              <Link
                tabIndex={0}
                onClick={() => setMenu(false)}
                aria-current={pathname === item.href ? "page" : undefined}
                className={
                  pathname === item.href ? "nav-item active" : "nav-item"
                }
                href={item.href}
              >
                <item.icon size={18} strokeWidth={1.6} />
                {item.label}
              </Link>
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="privacy-label">
            <ShieldCheck size={16} />
            <span>Synthetic by design</span>
          </div>
          <p>
            Independent. No employer affiliation.
            <br />
            AI is off.
          </p>
          <a tabIndex={0} href="/settings" className="help-link">
            <CircleHelp size={16} /> Workspace guide <ArrowUpRight size={13} />
          </a>
        </div>
      </aside>
      {menu && (
        <button
          tabIndex={0}
          className="menu-backdrop"
          aria-label="Dismiss navigation"
          onClick={() => setMenu(false)}
        />
      )}
      <div className="main-column">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              tabIndex={0}
              className="icon-button mobile-only"
              aria-label="Open navigation"
              onClick={() => setMenu(true)}
            >
              <Menu size={21} />
            </button>
            <span>Workspace</span>
            <ChevronRight size={13} />
            <strong>{current}</strong>
          </div>
          <div className="account">
            <span className="environment-tag">SYNTHETIC</span>
            <span className="account-avatar" aria-hidden="true">
              {me.display_name.slice(0, 1)}
            </span>
            <span className="account-name">
              {me.display_name}
              <small>{me.grants.map((g) => g.role).join(" · ")}</small>
            </span>
            <button
              tabIndex={0}
              onClick={logout}
              className="icon-button"
              aria-label="Sign out"
            >
              <LogOut size={17} />
            </button>
          </div>
        </header>
        <main tabIndex={-1} id="main-content" className="page-content">
          {logoutError && (
            <div role="alert" className="error-box">
              {logoutError}
            </div>
          )}
          {children(me)}
        </main>
        <footer className="page-footer">
          <span>
            MERIDIAN <span className="dot">·</span> Independent workforce
            intelligence
          </span>
          <span>Entirely synthetic data</span>
        </footer>
      </div>
    </div>
  );
}
