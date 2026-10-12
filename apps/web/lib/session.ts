export function logoutSucceeded(response: Pick<Response, "ok" | "status">) {
  // A stale page can send a logout after the server has already expired or
  // revoked the session. That is a successful signed-out outcome.
  return response.ok || response.status === 401;
}
