import createClient from "openapi-fetch";
import type { paths, components } from "../../../packages/contracts/api";
export const api = createClient<paths>({
  baseUrl: typeof window === "undefined" ? "" : window.location.origin,
  credentials: "same-origin",
  fetch: (request) => fetch(request),
});
export type Me = components["schemas"]["Me"];
export type Overview = components["schemas"]["Overview"];

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function getMe(): Promise<Me> {
  const { data, error, response } = await api.GET("/api/v1/me");
  if (error || !data)
    throw new ApiError(
      response.status,
      error?.detail || "Unable to load your session.",
    );
  return data;
}

export async function getOverview(): Promise<Overview> {
  const { data, error, response } = await api.GET("/api/v1/overview");
  if (error || !data)
    throw new ApiError(
      response.status,
      error?.detail || "Unable to load the workspace.",
    );
  return data;
}
