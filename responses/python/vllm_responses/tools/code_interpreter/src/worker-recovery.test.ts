import type { Subprocess } from "bun";
import { expect, test } from "bun:test";

import type { ExecutionResult, HealthResponse } from "./types";
import { PYODIDE_CACHE_DIR, waitForServerReady } from "./test-utils";

const TEST_PORT = 8788;
const SERVER_URL = `http://localhost:${TEST_PORT}`;

async function waitForHealthyWorkerPool(timeoutMs: number = 60_000): Promise<HealthResponse> {
  const startTime = Date.now();

  while (Date.now() - startTime < timeoutMs) {
    const response = await fetch(`${SERVER_URL}/health`);
    expect(response.status).toBe(200);
    const health = (await response.json()) as HealthResponse;
    if (health.status === "healthy" && health.ready_worker_count === health.configured_worker_count && health.pyodide_loaded) {
      return health;
    }
    await Bun.sleep(500);
  }

  throw new Error("Worker pool did not recover to healthy state within timeout");
}

test("worker pool respawns after fatal runtime termination", async () => {
  let serverProc: Subprocess | null = null;

  try {
    serverProc = Bun.spawn(["bun", "src/index.ts", "--port", String(TEST_PORT), "--workers", "2", "--pyodide-cache", PYODIDE_CACHE_DIR], {
      stdout: "inherit",
      stderr: "inherit",
    });
    const initialHealth = await waitForServerReady(SERVER_URL);
    expect(initialHealth.status).toBe("healthy");
    expect(initialHealth.ready_worker_count).toBe(2);
    expect(initialHealth.configured_worker_count).toBe(2);

    const fatalResponse = await fetch(`${SERVER_URL}/python`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: "raise SystemExit()" }),
    });
    expect(fatalResponse.status).toBe(500);
    const fatalBody = (await fatalResponse.json()) as { status: string; error: string };
    expect(fatalBody.status).toBe("error");
    expect(fatalBody.error).toContain("Python runtime terminated");

    const recoveryHealth = await waitForHealthyWorkerPool();
    expect(recoveryHealth.ready_worker_count).toBe(2);
    expect(recoveryHealth.configured_worker_count).toBe(2);

    const resultResponse = await fetch(`${SERVER_URL}/python`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: "1 + 1" }),
    });
    expect(resultResponse.status).toBe(200);
    const result = (await resultResponse.json()) as ExecutionResult;
    expect(result.status).toBe("success");
    expect(result.result).toBe("2");
  } finally {
    if (serverProc) {
      serverProc.kill();
      await serverProc.exited;
    }
  }
}, 120_000);
