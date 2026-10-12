import os from "node:os";
export function measurementEnvironment() {
  return {
    platform: os.platform(),
    release: os.release(),
    architecture: os.arch(),
    cpu: os.cpus()[0]?.model || "unobserved",
    cpu_cores: os.cpus().length,
    memory_bytes: os.totalmem(),
    execution:
      process.env.GITHUB_ACTIONS === "true"
        ? "github-actions-with-postgresql-service"
        : process.env.MERIDIAN_E2E_RUNTIME === "compose"
          ? "local-compose"
          : "native-local",
  };
}
