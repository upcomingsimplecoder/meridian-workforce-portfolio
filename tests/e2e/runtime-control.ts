import { execFile, spawn } from "node:child_process";
import { mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import { promisify } from "node:util";

const execute = promisify(execFile);
export const containerRuntime = process.env.MERIDIAN_E2E_RUNTIME === "compose";
const project = process.env.MERIDIAN_E2E_COMPOSE_PROJECT || "meridian";
if (!/^meridian(?:-[a-z0-9]+)*$/.test(project))
  throw new Error("Fault controls require a project-owned Compose name");
const compose = [
  "compose",
  "--project-name",
  project,
  "--env-file",
  ".local/compose.env",
  "-f",
  "infra/compose.yaml",
];
type Worker = "outbox" | "analytics";
const service = (worker: Worker) =>
  worker === "outbox" ? "worker" : "analytics-worker";

async function docker(args: string[]) {
  return execute("docker", args, {
    timeout: 30000,
    maxBuffer: 8 * 1024 * 1024,
  });
}
async function inApi(program: string, args: string[] = []) {
  return (
    await docker([
      ...compose,
      "exec",
      "-T",
      "api",
      "python",
      "-c",
      program,
      ...args,
    ])
  ).stdout;
}

export async function pauseWorker(worker: Worker) {
  if (containerRuntime) {
    const id = (
      await docker([...compose, "ps", "-q", service(worker)])
    ).stdout.trim();
    if (!/^[0-9a-f]{64}$/.test(id))
      throw new Error("Expected one running owned container");
    const details = JSON.parse((await docker(["inspect", id])).stdout)[0];
    if (
      details.Config.Labels["com.docker.compose.project"] !== project ||
      details.Config.Labels["com.docker.compose.service"] !== service(worker)
    )
      throw new Error("Refusing to freeze an unrelated container");
    await docker([...compose, "pause", service(worker)]);
    return async () => {
      await docker([...compose, "unpause", service(worker)]);
    };
  }
  const pid = Number(
    (
      await readFile(
        `.local/${worker === "outbox" ? "worker" : "analytics-worker"}.pid`,
        "utf8",
      )
    ).trim(),
  );
  if (!Number.isInteger(pid) || pid <= 1)
    throw new Error("Invalid owned worker PID");
  const command = (await execute("ps", ["-p", String(pid), "-o", "command="]))
    .stdout;
  const module =
    worker === "outbox" ? "apps.worker.run" : "apps.worker.analytics";
  if (!command.includes(`-m ${module}`))
    throw new Error("Refusing to stop an unrelated PID");
  process.kill(pid, "SIGSTOP");
  return async () => {
    process.kill(pid, "SIGCONT");
  };
}

export async function takeObjectOffline(digest: string) {
  if (!/^[0-9a-f]{64}$/.test(digest))
    throw new Error("Invalid private content address");
  if (containerRuntime) {
    const bytes = await inApi(
      "import base64,sys; from meridian.object_store import object_path,read_private; p=object_path(sys.argv[1]); b=read_private(sys.argv[1]); p.rename(str(p)+'.p08-outage'); print(base64.b64encode(b).decode())",
      [digest],
    );
    return {
      original: Buffer.from(bytes.trim(), "base64"),
      restore: async () => {
        await inApi(
          "import sys; from pathlib import Path; from meridian.object_store import object_path; p=object_path(sys.argv[1]); Path(str(p)+'.p08-outage').rename(p)",
          [digest],
        );
      },
    };
  }
  const path = `.local/objects/${digest}`;
  const original = await readFile(path);
  await rename(path, path + ".p08-outage");
  return {
    original,
    restore: async () => {
      await rename(path + ".p08-outage", path);
    },
  };
}

function validateBarrier(path: string) {
  const root = containerRuntime
    ? "/data/analytics-barrier-"
    : resolve(".local/analytics-barrier-");
  if (
    !path.startsWith(root) ||
    !/^[a-zA-Z0-9-]+$/.test(path.slice(root.length))
  )
    throw new Error("Barrier must stay in the project-owned test directory");
}
export async function createAnalyticalBarrier() {
  if (!containerRuntime)
    return resolve(await mkdtemp(".local/analytics-barrier-"));
  const path = "/data/analytics-barrier-" + randomUUID().replaceAll("-", "");
  await inApi(
    "import sys; from pathlib import Path; Path(sys.argv[1]).mkdir(mode=0o700)",
    [path],
  );
  return path;
}
export async function readBarrier(path: string, run: string) {
  validateBarrier(path);
  if (!/^[0-9a-f-]{36}$/.test(run))
    throw new Error("Invalid actual analytical run ID");
  if (!containerRuntime) {
    try {
      return await readFile(`${path}/${run}.locked`, "utf8");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw error;
    }
  }
  const output = await inApi(
    "import sys,json; from pathlib import Path; p=Path(sys.argv[1])/ (sys.argv[2]+'.locked'); print(json.dumps(p.read_text() if p.is_file() else None))",
    [path, run],
  );
  return JSON.parse(output) as string | null;
}
export async function releaseBarrier(path: string) {
  validateBarrier(path);
  if (!containerRuntime)
    return writeFile(`${path}/release`, "release real SQL work", {
      mode: 0o600,
    });
  await inApi(
    "import sys; from pathlib import Path; p=Path(sys.argv[1])/'release'; p.write_text('release real SQL work'); p.chmod(0o600)",
    [path],
  );
}
export async function removeBarrier(path: string) {
  validateBarrier(path);
  if (!containerRuntime) return rm(path, { recursive: true, force: true });
  await inApi("import sys,shutil; shutil.rmtree(sys.argv[1])", [path]);
}

export function startAnalyticalWorker(
  path: string,
  runs: string[],
  index: number,
) {
  validateBarrier(path);
  if (
    runs.length !== 2 ||
    runs.some((run) => !/^[0-9a-f-]{36}$/.test(run)) ||
    ![0, 1].includes(index)
  )
    throw new Error("Overlap probe requires two persisted analytical run IDs");
  const name = `${project}-overlap-${path.split("analytics-barrier-")[1].toLowerCase()}-${index}`;
  const child = containerRuntime
    ? spawn(
        "docker",
        [
          ...compose,
          "run",
          "--rm",
          "--no-deps",
          "--name",
          name,
          "-e",
          `MERIDIAN_TEST_BARRIER=${path}`,
          "-e",
          `MERIDIAN_TEST_RUNS=${runs.join(",")}`,
          "-e",
          "MERIDIAN_TEST_CONTAINER_BARRIER=1",
          "analytics-worker",
          "python",
          "scripts/analytics_overlap_worker.py",
          "--once",
        ],
        { stdio: "ignore" },
      )
    : spawn(
        ".venv/bin/python",
        ["scripts/analytics_overlap_worker.py", "--once"],
        {
          stdio: "ignore",
          env: {
            ...process.env,
            MERIDIAN_TEST_BARRIER: path,
            MERIDIAN_TEST_RUNS: runs.join(","),
          },
        },
      );
  return {
    child,
    terminate: async () => {
      if (child.exitCode !== null) return;
      if (containerRuntime) await docker(["stop", "--time", "5", name]);
      else child.kill("SIGTERM");
    },
  };
}
