import type { Subprocess } from "bun";
import { expect, test } from "bun:test";

import type { ExecutionResult, HealthResponse } from "./types";
import { PYODIDE_CACHE_DIR, waitForServerReady } from "./test-utils";

const TEST_PORT = 8785;
const SERVER_URL = `http://localhost:${TEST_PORT}`;
const AUX_HTTP_PORT = 8787;
const AUX_HTTP_URL = `http://127.0.0.1:${AUX_HTTP_PORT}/json`;

function startAuxHttpServer(): ReturnType<typeof Bun.serve> {
  return Bun.serve({
    port: AUX_HTTP_PORT,
    fetch(req) {
      const url = new URL(req.url);
      if (url.pathname === "/json") {
        return Response.json({ ok: true, source: "local-test-server" });
      }
      return new Response("Not Found", { status: 404 });
    },
  });
}

async function executePython(
  code: string,
  resetGlobals?: boolean,
): Promise<{ status: number; body: ExecutionResult | { status: string; error: string } }> {
  const response = await fetch(`${SERVER_URL}/python`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(resetGlobals === undefined ? { code } : { code, reset_globals: resetGlobals }),
  });
  return {
    status: response.status,
    body: (await response.json()) as ExecutionResult | { status: string; error: string },
  };
}

test("server endpoint contract semantics", async () => {
  let serverProc: Subprocess | null = null;
  let auxServer: ReturnType<typeof Bun.serve> | null = null;

  try {
    auxServer = startAuxHttpServer();

    serverProc = Bun.spawn(["bun", "src/index.ts", "--port", String(TEST_PORT), "--pyodide-cache", PYODIDE_CACHE_DIR], {
      stdout: "inherit",
      stderr: "inherit",
    });
    await waitForServerReady(SERVER_URL);

    const stdoutResult = await executePython('print("P1"); print("P2"); 2+2');
    expect(stdoutResult.status).toBe(200);
    expect((stdoutResult.body as ExecutionResult).status).toBe("success");
    expect((stdoutResult.body as ExecutionResult).stdout).toBe("P1\nP2\n");
    expect((stdoutResult.body as ExecutionResult).stderr).toBe("");
    expect((stdoutResult.body as ExecutionResult).result).toBe("4");

    const errorResult = await executePython("raise ValueError('test error')");
    expect(errorResult.status).toBe(200);
    expect((errorResult.body as ExecutionResult).status).toBe("exception");
    expect((errorResult.body as ExecutionResult).result).toContain("ValueError");
    expect((errorResult.body as ExecutionResult).result).toContain("test error");

    const invalidResponse = await fetch(`${SERVER_URL}/python`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ invalid: "field" }),
    });
    expect(invalidResponse.status).toBe(400);
    const invalidBody = (await invalidResponse.json()) as { status: string; error: string };
    expect(invalidBody.status).toBe("error");
    expect(invalidBody.error).toContain("code");

    const setVarResult = await executePython("x = 42");
    expect(setVarResult.status).toBe(200);
    expect((setVarResult.body as ExecutionResult).result).toBe(null);

    const accessVarResult = await executePython("x");
    expect(accessVarResult.status).toBe(200);
    expect((accessVarResult.body as ExecutionResult).status).toBe("exception");
    expect((accessVarResult.body as ExecutionResult).result).toContain("NameError");

    const sharedStateSetResult = await executePython("y = 99", false);
    expect(sharedStateSetResult.status).toBe(200);
    expect((sharedStateSetResult.body as ExecutionResult).status).toBe("success");
    expect((sharedStateSetResult.body as ExecutionResult).result).toBe(null);

    const sharedStateAccessResult = await executePython("y", false);
    expect(sharedStateAccessResult.status).toBe(200);
    expect((sharedStateAccessResult.body as ExecutionResult).status).toBe("success");
    expect((sharedStateAccessResult.body as ExecutionResult).result).toBe("99");

    const multilineResult = await executePython(`
def x():
    return 300

x()
    `);
    expect(multilineResult.status).toBe(200);
    expect((multilineResult.body as ExecutionResult).status).toBe("success");
    expect((multilineResult.body as ExecutionResult).result).toBe("300");

    const requestsResult = await executePython(`import requests; response = requests.get("${AUX_HTTP_URL}"); response.status_code`);
    expect(requestsResult.status).toBe(200);
    expect((requestsResult.body as ExecutionResult).status).toBe("success");
    expect((requestsResult.body as ExecutionResult).result).toBe("200");

    const httpxResult = await executePython(`import httpx; response = httpx.get("${AUX_HTTP_URL}"); response.status_code`);
    expect(httpxResult.status).toBe(200);
    expect((httpxResult.body as ExecutionResult).status).toBe("success");
    expect((httpxResult.body as ExecutionResult).result).toBe("200");

    for (const [label, code] of [
      ["exit()", "exit()"],
      ["quit()", "quit()"],
      ["sys.exit()", "import sys; sys.exit()"],
    ] as const) {
      const exitHelperResult = await executePython(code);
      expect(exitHelperResult.status).toBe(200);
      expect((exitHelperResult.body as ExecutionResult).status).toBe("exception");
      expect((exitHelperResult.body as ExecutionResult).result).toContain("disabled");

      const healthRes = await fetch(`${SERVER_URL}/health`);
      expect(healthRes.status).toBe(200);
      const health = (await healthRes.json()) as HealthResponse;
      expect(health.status).toBe("healthy");
      expect(health.pyodide_loaded).toBe(true);
    }

    const fatalResult = await executePython("raise SystemExit()");
    expect(fatalResult.status).toBe(500);
    expect((fatalResult.body as { status: string; error: string }).status).toBe("error");
    expect((fatalResult.body as { status: string; error: string }).error).toContain("Python runtime terminated");

    const recoveredHealth = await waitForServerReady(SERVER_URL);
    expect(recoveredHealth.status).toBe("healthy");

    const recoveredResult = await executePython("1 + 1");
    expect(recoveredResult.status).toBe(200);
    expect((recoveredResult.body as ExecutionResult).status).toBe("success");
    expect((recoveredResult.body as ExecutionResult).result).toBe("2");
  } finally {
    if (serverProc) {
      serverProc.kill();
      await serverProc.exited;
    }
    if (auxServer) {
      auxServer.stop();
    }
  }
}, 120000);
