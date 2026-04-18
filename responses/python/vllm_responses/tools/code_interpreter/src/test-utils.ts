import { existsSync } from "fs";
import { homedir } from "os";
import { join } from "path";

import { expect } from "bun:test";

import type { ExecutionResult, HealthResponse } from "./types";

function expandHome(path: string): string {
  if (path === "~") {
    return homedir();
  }
  if (path.startsWith("~/")) {
    return join(homedir(), path.slice(2));
  }
  return path;
}

export const PYODIDE_CACHE_DIR = expandHome(process.env.VR_PYODIDE_CACHE_DIR?.trim() || join(homedir(), ".pyodide-env"));

export async function buildCompiledBinary(cwd: string): Promise<string> {
  const buildProc = Bun.spawn(["bun", "run", "build"], {
    cwd,
    stdout: "inherit",
    stderr: "inherit",
  });
  const buildExitCode = await buildProc.exited;
  expect(buildExitCode).toBe(0);

  const binaryPath = join(cwd, "woma");
  expect(existsSync(binaryPath)).toBe(true);
  return binaryPath;
}

export async function waitForServerReady(serverUrl: string, timeoutMs: number = 180_000): Promise<HealthResponse> {
  const startTime = Date.now();
  const pollIntervalMs = 1_000;

  while (Date.now() - startTime < timeoutMs) {
    try {
      const res = await fetch(`${serverUrl}/health`);
      if (res.ok) {
        const health = (await res.json()) as HealthResponse;
        if (health.pyodide_loaded) {
          return health;
        }
      }
    } catch {
      // Server not ready yet.
    }

    await Bun.sleep(pollIntervalMs);
  }

  throw new Error(`Server ${serverUrl} did not become ready within ${timeoutMs}ms`);
}

export async function executePython(serverUrl: string, code: string): Promise<ExecutionResult> {
  const execRes = await fetch(`${serverUrl}/python`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  expect(execRes.status).toBe(200);
  return (await execRes.json()) as ExecutionResult;
}

export async function assertStartupAndExecution(serverUrl: string): Promise<void> {
  const initialHealth = await waitForServerReady(serverUrl);
  expect(initialHealth.status).toBe("healthy");
  expect(initialHealth.pyodide_loaded).toBe(true);

  const result = await executePython(serverUrl, "x = 1 + 1; x");
  expect(result.status).toBe("success");
  expect(result.stdout).toBe("");
  expect(result.stderr).toBe("");
  expect(result.result).toBe("2");
  expect(result.execution_time_ms).toBeGreaterThan(0);

  const finalHealthRes = await fetch(`${serverUrl}/health`);
  expect(finalHealthRes.status).toBe(200);
  const finalHealth = (await finalHealthRes.json()) as HealthResponse;
  expect(finalHealth.execution_count).toBeGreaterThan(0);
}
