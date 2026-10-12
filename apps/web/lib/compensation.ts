import type { components } from "../../../packages/contracts/api";
import { api, ApiError, type Me } from "./api";
import { formatMoney } from "./workforce";

export type Cycle = components["schemas"]["CycleView"];
export type Proposal = components["schemas"]["ProposalView"];
export type CycleRequest = components["schemas"]["CycleCreate"];
export type Options = components["schemas"]["CompOptions"];
export type AwardRequest = components["schemas"]["AwardRequest"];
export type ReviewRequest = components["schemas"]["CompReview"];
export type Evidence = components["schemas"]["CompEvidenceView"];
export type CycleAction = "launch" | "lock" | "reconcile" | "retry-export";

function result<T>({
  data,
  error,
  response,
}: {
  data?: T;
  error?: { detail?: string; request_id?: string };
  response: Response;
}): T {
  if (!response.ok || data === undefined)
    throw new ApiError(
      response.status,
      `${error?.detail || "Compensation request failed."}${error?.request_id ? ` Reference: ${error.request_id}` : ""}`,
    );
  return data;
}

export function compMoney(
  minor: string,
  currency: string,
  exponent = currency === "JPY" ? 0 : currency === "KWD" ? 3 : 2,
) {
  if (
    currency !== "USD" &&
    currency !== "EUR" &&
    currency !== "GBP" &&
    currency !== "JPY" &&
    currency !== "KWD"
  )
    throw new Error("Unsupported monetary currency.");
  return formatMoney({ minor_units: minor, currency, exponent });
}

export function moneyInput(minor: string, exponent: number): string {
  if (!/^(0|[1-9][0-9]{0,19})$/.test(minor) || ![0, 2, 3].includes(exponent))
    throw new Error("Invalid exact monetary amount.");
  if (!exponent) return minor;
  const digits = minor.padStart(exponent + 1, "0");
  return `${digits.slice(0, -exponent)}.${digits.slice(-exponent)}`;
}

export function parseMoneyInput(value: string, exponent: number): string {
  if (![0, 2, 3].includes(exponent) || !/^\d+(\.\d+)?$/.test(value))
    throw new Error(
      "Use a non-negative amount with a decimal point and no separators.",
    );
  const [whole, fraction = ""] = value.split(".");
  if (fraction.length > exponent)
    throw new Error(`This currency supports ${exponent} decimal places.`);
  const minor = BigInt(whole + fraction.padEnd(exponent, "0")).toString();
  if (minor.length > 20)
    throw new Error("Amount exceeds the supported monetary range.");
  return minor;
}

export function percent(rate: string): string {
  const [whole, fraction = ""] = rate.split(".");
  const digits = (BigInt(whole + fraction) * 100n)
    .toString()
    .padStart(fraction.length + 1, "0");
  return (
    (fraction.length
      ? `${digits.slice(0, -fraction.length)}.${digits.slice(-fraction.length)}`.replace(
          /\.?0+$/,
          "",
        )
      : digits) + "%"
  );
}

export async function getCycles(cursor?: string, search?: string) {
  return result(
    await api.GET("/api/v1/compensation/cycles", {
      params: { query: { cursor, search: search || undefined, limit: 10 } },
    }),
  );
}
export async function getOptions(search = "") {
  return result(
    await api.GET("/api/v1/compensation/options", {
      params: { query: { source_search: search || undefined } },
    }),
  );
}
export async function getCycle(id: string) {
  return result(
    await api.GET("/api/v1/compensation/cycles/{cycle_id}", {
      params: { path: { cycle_id: id } },
    }),
  );
}
export async function getProposals(
  id: string,
  cursor?: string,
  search?: string,
) {
  return result(
    await api.GET("/api/v1/compensation/cycles/{cycle_id}/proposals", {
      params: {
        path: { cycle_id: id },
        query: { cursor, search: search || undefined, limit: 25 },
      },
    }),
  );
}
export async function getProposal(id: string, revision?: number) {
  return result(
    await api.GET("/api/v1/compensation/proposals/{proposal_id}", {
      params: { path: { proposal_id: id }, query: { revision } },
    }),
  );
}
export async function getEligibility(id: string, cursor?: string) {
  return result(
    await api.GET("/api/v1/compensation/cycles/{cycle_id}/eligibility", {
      params: { path: { cycle_id: id }, query: { cursor, limit: 100 } },
    }),
  );
}
export async function getEvidence(cursor?: string) {
  return result(
    await api.GET("/api/v1/compensation/evidence", {
      params: { query: { cursor, limit: 25 } },
    }),
  );
}
export async function getExport(id: string) {
  return result(
    await api.GET("/api/v1/compensation/cycles/{cycle_id}/export", {
      params: { path: { cycle_id: id } },
    }),
  );
}
export async function createCycle(me: Me, body: CycleRequest, key: string) {
  return result(
    await api.POST("/api/v1/compensation/cycles", {
      body,
      headers: { "X-CSRF-Token": me.csrf_token },
      params: { header: { "Idempotency-Key": key } },
    }),
  );
}
export async function cycleCommand(
  me: Me,
  cycle: Cycle,
  action: CycleAction,
  reason: string,
  key: string,
) {
  const options = {
    body: { reason },
    headers: { "X-CSRF-Token": me.csrf_token },
    params: {
      path: { cycle_id: cycle.id },
      header: { "Idempotency-Key": key, "If-Match": cycle.version },
    },
  };
  switch (action) {
    case "launch":
      return result(
        await api.POST(
          "/api/v1/compensation/cycles/{cycle_id}/launch",
          options,
        ),
      );
    case "lock":
      return result(
        await api.POST("/api/v1/compensation/cycles/{cycle_id}/lock", options),
      );
    case "reconcile":
      return result(
        await api.POST(
          "/api/v1/compensation/cycles/{cycle_id}/reconcile",
          options,
        ),
      );
    case "retry-export":
      return result(
        await api.POST(
          "/api/v1/compensation/cycles/{cycle_id}/retry-export",
          options,
        ),
      );
  }
}
export type ProposalCommand =
  | { action: "award"; body: AwardRequest }
  | { action: "submit"; body: { reason: string } }
  | { action: "review"; body: { reason: string; revision_id: string } }
  | { action: "decisions"; body: ReviewRequest };
export async function proposalCommand(
  me: Me,
  proposal: Proposal,
  command: ProposalCommand,
  key: string,
) {
  const options = {
    headers: { "X-CSRF-Token": me.csrf_token },
    params: {
      path: { proposal_id: proposal.id },
      header: { "Idempotency-Key": key, "If-Match": proposal.version },
    },
  };
  switch (command.action) {
    case "award":
      return result(
        await api.POST("/api/v1/compensation/proposals/{proposal_id}/award", {
          ...options,
          body: command.body,
        }),
      );
    case "submit":
      return result(
        await api.POST("/api/v1/compensation/proposals/{proposal_id}/submit", {
          ...options,
          body: command.body,
        }),
      );
    case "review":
      return result(
        await api.POST("/api/v1/compensation/proposals/{proposal_id}/review", {
          ...options,
          body: command.body,
        }),
      );
    case "decisions":
      return result(
        await api.POST(
          "/api/v1/compensation/proposals/{proposal_id}/decisions",
          { ...options, body: command.body },
        ),
      );
  }
}

export async function downloadCompExport(id: string) {
  const manifest = await getExport(id);
  const url = new URL(
    `/api/v1/compensation/cycles/${id}/download`,
    window.location.origin,
  );
  url.searchParams.set("token", manifest.download_token);
  const response = await fetch(url, { credentials: "same-origin" });
  if (!response.ok)
    throw new ApiError(
      response.status,
      "The current authorized export could not be downloaded. Refresh your access and try again.",
    );
  const href = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = `meridian-comp-${id.slice(0, 8)}-v${manifest.version}.csv`;
  anchor.click();
  URL.revokeObjectURL(href);
}
