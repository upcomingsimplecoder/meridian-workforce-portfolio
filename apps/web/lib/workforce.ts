import type { components } from "../../../packages/contracts/api";
import { api, ApiError } from "./api";
export type Person =
  | components["schemas"]["PersonPublic"]
  | components["schemas"]["PersonWithPay"];
export type Money = components["schemas"]["Money"];
export type Snapshot = components["schemas"]["SnapshotView"];
export type WorkforceSummary = components["schemas"]["WorkforceSummary"];
export type HistoricalFilters = { snapshot_id: string; as_of: string };
export type PeopleFilters = HistoricalFilters & {
  q?: string;
  org?: string;
  cursor?: string;
  limit?: number;
};

function result<T>({ data, response }: { data?: T; response: Response }): T {
  if (!response.ok || data === undefined)
    throw new ApiError(
      response.status,
      response.status === 401
        ? "Your session has expired. Sign in again."
        : response.status === 403
          ? "Your current permissions do not allow this view."
          : "The workforce source could not be loaded. Retry or contact your data steward with the request ID.",
    );
  return data;
}
export async function getSnapshots(search = "", snapshotId?: string) {
  return result(
    await api.GET("/api/v1/snapshots", {
      params: {
        query: { source_search: search || undefined, snapshot_id: snapshotId },
      },
    }),
  );
}
export async function getPeople(filters: PeopleFilters) {
  return result(
    await api.GET("/api/v1/people", { params: { query: filters } }),
  );
}
export async function getSummary(filters: HistoricalFilters) {
  return result(
    await api.GET("/api/v1/workforce/summary", { params: { query: filters } }),
  );
}
export async function getOrgTree(filters: HistoricalFilters) {
  return result(
    await api.GET("/api/v1/org-units", { params: { query: filters } }),
  );
}
export async function getPerson(id: string, filters: HistoricalFilters) {
  return result(
    await api.GET("/api/v1/people/{employee_id}", {
      params: { path: { employee_id: id }, query: filters },
    }),
  );
}
export async function getHistory(id: string, filters: HistoricalFilters) {
  return result(
    await api.GET("/api/v1/people/{employee_id}/history", {
      params: { path: { employee_id: id }, query: filters },
    }),
  );
}
export async function exportPeople(filters: PeopleFilters) {
  const { data, response } = await api.GET("/api/v1/people/export", {
    params: { query: filters },
    parseAs: "blob",
  });
  if (!response.ok || !data)
    throw new ApiError(
      response.status,
      "Export was denied or could not be generated. Refresh your access and try again.",
    );
  const url = URL.createObjectURL(data);
  const link = document.createElement("a");
  link.href = url;
  link.download = "meridian-synthetic-workforce.csv";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
// Never coerce minor units to Number: large integers and three-decimal currencies must remain exact.
export function formatMoney(money: Money): string {
  const integer = BigInt(money.minor_units);
  const sign = integer < 0n ? "−" : "";
  const digits = (integer < 0n ? -integer : integer)
    .toString()
    .padStart(money.exponent + 1, "0");
  const whole = money.exponent ? digits.slice(0, -money.exponent) : digits;
  const fraction = money.exponent ? "." + digits.slice(-money.exponent) : "";
  return `${money.currency} ${sign}${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${fraction}`;
}
