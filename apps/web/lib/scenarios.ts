import type { components } from "../../../packages/contracts/api";
import { api, ApiError, type Me } from "./api";
import { formatMoney } from "./workforce";

export type Scenario = components["schemas"]["ScenarioDetail"];
export type ScenarioSummary = components["schemas"]["ScenarioSummary"];
export type ScenarioOptions = components["schemas"]["ScenarioOptions"];
export type ScenarioRequest = components["schemas"]["ScenarioRequest"];
export type Forecast = components["schemas"]["ForecastView"];
export type CostResult = components["schemas"]["CostResult"];
export type Operation =
  components["schemas"]["OperationsRequest"]["operations"][number];
export type Command =
  | { action: "branches"; body: components["schemas"]["BranchRequest"] }
  | { action: "operations"; body: components["schemas"]["OperationsRequest"] }
  | { action: "decisions"; body: components["schemas"]["ReviewRequest"] }
  | {
      action: "undo" | "submit" | "review" | "promote" | "retry";
      body: components["schemas"]["ReasonRequest"];
    };

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
      `${error?.detail || "Scenario request failed."}${error?.request_id ? ` Reference: ${error.request_id}` : ""}`,
    );
  return data;
}
export function money(
  minor: string,
  context: Pick<CostResult, "currency" | "exponent">,
) {
  return formatMoney({
    minor_units: minor,
    currency: context.currency,
    exponent: context.exponent,
  });
}
export async function getScenarios(cursor?: string, search?: string) {
  return result(
    await api.GET("/api/v1/scenarios", {
      params: { query: { limit: 10, cursor, search: search || undefined } },
    }),
  );
}
export async function getScenarioOptions(search = "") {
  return result(
    await api.GET("/api/v1/scenarios/options", {
      params: { query: { source_search: search || undefined } },
    }),
  );
}
export async function getScenario(id: string) {
  return result(
    await api.GET("/api/v1/scenarios/{scenario_id}", {
      params: { path: { scenario_id: id } },
    }),
  );
}
export async function getForecast(id: string, revision?: number) {
  return result(
    await api.GET("/api/v1/scenarios/{scenario_id}/forecast", {
      params: { path: { scenario_id: id }, query: { revision } },
    }),
  );
}
export async function compareScenarios(ids: string[]) {
  return result(
    await api.GET("/api/v1/scenarios/compare", { params: { query: { ids } } }),
  );
}
export async function createScenario(
  me: Me,
  body: ScenarioRequest,
  key: string,
) {
  return result(
    await api.POST("/api/v1/scenarios", {
      body,
      headers: { "X-CSRF-Token": me.csrf_token },
      params: { header: { "Idempotency-Key": key } },
    }),
  );
}
export async function runCommand(
  me: Me,
  scenario: Scenario,
  command: Command,
  key: string,
) {
  const params = {
    path: { scenario_id: scenario.id },
    header: { "Idempotency-Key": key, "If-Match": scenario.version },
  };
  const headers = { "X-CSRF-Token": me.csrf_token };
  switch (command.action) {
    case "branches":
      return result(
        await api.POST("/api/v1/scenarios/{scenario_id}/branches", {
          body: command.body,
          params,
          headers,
        }),
      );
    case "operations":
      return result(
        await api.POST("/api/v1/scenarios/{scenario_id}/operations", {
          body: command.body,
          params,
          headers,
        }),
      );
    case "decisions":
      return result(
        await api.POST("/api/v1/scenarios/{scenario_id}/decisions", {
          body: command.body,
          params,
          headers,
        }),
      );
    default:
      return result(
        await api.POST(`/api/v1/scenarios/{scenario_id}/${command.action}`, {
          body: command.body,
          params,
          headers,
        }),
      );
  }
}
export async function downloadScenario(
  scenario: Scenario,
  revision: number,
  kind: "csv" | "manifest",
) {
  const response = await fetch(
    `/api/v1/scenarios/${encodeURIComponent(scenario.id)}/${kind === "csv" ? `export?revision=${revision}` : "manifest"}`,
    { credentials: "same-origin", cache: "no-store" },
  );
  if (!response.ok) {
    const problem = await response.json();
    throw new ApiError(
      response.status,
      problem.detail || "Export is unavailable in your current scope.",
    );
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `meridian-scenario-${scenario.id}-r${revision}.${kind === "csv" ? "csv" : "json"}`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
