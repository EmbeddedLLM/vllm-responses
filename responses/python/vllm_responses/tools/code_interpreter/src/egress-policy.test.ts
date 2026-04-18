import { existsSync, mkdtempSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import type { Subprocess } from "bun";
import { expect, test } from "bun:test";

import type { ExecutionResult } from "./types";
import { PYODIDE_CACHE_DIR, buildCompiledBinary, waitForServerReady } from "./test-utils";

const PYODIDE_VERSION = "0.29.1";
const VERSION_MARKER = ".pyodide_version";
const CACHE_DIR = PYODIDE_CACHE_DIR;

let cacheReady: Promise<void> | null = null;

function writePolicy(name: string, policy: object): string {
  const dir = mkdtempSync(join(tmpdir(), `vllm-responses-egress-${name}-`));
  const policyPath = join(dir, "policy.json");
  writeFileSync(policyPath, JSON.stringify(policy), "utf8");
  return policyPath;
}

function startAuxHttpServer(port: number): ReturnType<typeof Bun.serve> {
  return Bun.serve({
    hostname: "127.0.0.1",
    port,
    fetch(req) {
      const url = new URL(req.url);
      if (url.pathname === "/json") {
        return Response.json({ ok: true });
      }
      return new Response("Not Found", { status: 404 });
    },
  });
}

async function ensurePyodideCacheReady(): Promise<void> {
  if (existsSync(join(CACHE_DIR, VERSION_MARKER))) {
    return;
  }
  if (cacheReady !== null) {
    return cacheReady;
  }
  cacheReady = (async () => {
    let serverProc: Subprocess | null = null;
    try {
      serverProc = Bun.spawn(["bun", "src/index.ts", "--port", "8790", "--pyodide-cache", CACHE_DIR], {
        stdout: "inherit",
        stderr: "inherit",
      });
      await waitForServerReady("http://localhost:8790", 180_000);
    } finally {
      if (serverProc) {
        serverProc.kill();
        await serverProc.exited;
      }
    }
  })();
  return cacheReady;
}

async function executePython(serverUrl: string, code: string): Promise<ExecutionResult> {
  const response = await fetch(`${serverUrl}/python`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  expect(response.status).toBe(200);
  return (await response.json()) as ExecutionResult;
}

async function withRuntime(cmd: string[], serverUrl: string, callback: () => Promise<void>, cwd: string = process.cwd()): Promise<void> {
  let serverProc: Subprocess | null = null;
  try {
    serverProc = Bun.spawn(cmd, {
      cwd,
      stdout: "inherit",
      stderr: "inherit",
    });
    await waitForServerReady(serverUrl, 120_000);
    await callback();
  } finally {
    if (serverProc) {
      serverProc.kill();
      await serverProc.exited;
    }
  }
}

function allowLoopbackPolicy(): object {
  return {
    mode: "allowlist",
    allowed_schemes: ["http"],
    block_ip_literals: false,
    block_private_networks: false,
    rules: [{ kind: "host", value: "127.0.0.1" }],
  };
}

function nonAllowlistedPolicy(): object {
  return {
    mode: "allowlist",
    allowed_schemes: ["http"],
    block_ip_literals: false,
    block_private_networks: false,
    rules: [{ kind: "host", value: "example.com" }],
  };
}

function denyLoopbackPolicy(): object {
  return {
    mode: "denylist",
    allowed_schemes: ["http"],
    block_ip_literals: false,
    block_private_networks: false,
    rules: [{ kind: "host", value: "127.0.0.1" }],
  };
}

test("source server egress policy allows allowlisted requests/httpx targets", async () => {
  await ensurePyodideCacheReady();
  const auxServer = startAuxHttpServer(8791);
  const policyPath = writePolicy("allow", allowLoopbackPolicy());
  try {
    await withRuntime(
      ["bun", "src/index.ts", "--port", "8792", "--pyodide-cache", CACHE_DIR, "--egress-policy-file", policyPath],
      "http://localhost:8792",
      async () => {
        const requestsResult = await executePython(
          "http://localhost:8792",
          'import requests; response = requests.get("http://127.0.0.1:8791/json"); response.json()["ok"]',
        );
        expect(requestsResult.status).toBe("success");
        expect(requestsResult.result).toBe("true");

        const httpxResult = await executePython(
          "http://localhost:8792",
          'import httpx; response = httpx.get("http://127.0.0.1:8791/json"); response.json()["ok"]',
        );
        expect(httpxResult.status).toBe("success");
        expect(httpxResult.result).toBe("true");
      },
    );
  } finally {
    auxServer.stop();
  }
}, 240_000);

test("source server egress policy blocks non-allowlisted and denylisted targets", async () => {
  await ensurePyodideCacheReady();
  const auxServer = startAuxHttpServer(8793);
  const nonAllowlistedPolicyPath = writePolicy("non-allowlisted", nonAllowlistedPolicy());
  const denyPolicyPath = writePolicy("deny", denyLoopbackPolicy());
  try {
    await withRuntime(
      ["bun", "src/index.ts", "--port", "8794", "--pyodide-cache", CACHE_DIR, "--egress-policy-file", nonAllowlistedPolicyPath],
      "http://localhost:8794",
      async () => {
        const result = await executePython("http://localhost:8794", 'import requests; requests.get("http://127.0.0.1:8793/json").status_code');
        expect(result.status).toBe("exception");
        expect(result.result).toContain("Code interpreter egress denied:");
        expect(result.result).toContain("reason=host_not_allowlisted");
      },
    );

    await withRuntime(
      ["bun", "src/index.ts", "--port", "8795", "--pyodide-cache", CACHE_DIR, "--egress-policy-file", denyPolicyPath],
      "http://localhost:8795",
      async () => {
        const result = await executePython("http://localhost:8795", 'import httpx; httpx.get("http://127.0.0.1:8793/json").status_code');
        expect(result.status).toBe("exception");
        expect(result.result).toContain("Code interpreter egress denied:");
        expect(result.result).toContain("reason=host_denylisted");
      },
    );
  } finally {
    auxServer.stop();
  }
}, 240_000);

test("source worker mode egress policy blocks special-use addresses", async () => {
  await ensurePyodideCacheReady();
  const auxServer = startAuxHttpServer(8796);
  const policyPath = writePolicy("special-use", {
    mode: "allowlist",
    allowed_schemes: ["http"],
    block_ip_literals: false,
    block_private_networks: true,
    rules: [{ kind: "host", value: "127.0.0.1" }],
  });
  try {
    await withRuntime(
      ["bun", "src/index.ts", "--port", "8797", "--workers", "2", "--pyodide-cache", CACHE_DIR, "--egress-policy-file", policyPath],
      "http://localhost:8797",
      async () => {
        const result = await executePython("http://localhost:8797", 'import requests; requests.get("http://127.0.0.1:8796/json").status_code');
        expect(result.status).toBe("exception");
        expect(result.result).toContain("Code interpreter egress denied:");
        expect(result.result).toContain("reason=special_use_address_blocked");
      },
    );
  } finally {
    auxServer.stop();
  }
}, 240_000);

test("denylist egress policy allows empty rules for scheme and internal-network-only policy", async () => {
  await ensurePyodideCacheReady();
  const auxServer = startAuxHttpServer(8802);
  const policyPath = writePolicy("deny-empty-rules", {
    mode: "denylist",
    allowed_schemes: ["http"],
    block_ip_literals: false,
    block_private_networks: true,
    rules: [],
  });
  try {
    await withRuntime(
      ["bun", "src/index.ts", "--port", "8803", "--pyodide-cache", CACHE_DIR, "--egress-policy-file", policyPath],
      "http://localhost:8803",
      async () => {
        const result = await executePython("http://localhost:8803", 'import requests; requests.get("http://127.0.0.1:8802/json").status_code');
        expect(result.status).toBe("exception");
        expect(result.result).toContain("Code interpreter egress denied:");
        expect(result.result).toContain("reason=special_use_address_blocked");
      },
    );
  } finally {
    auxServer.stop();
  }
}, 240_000);

test("compiled server and worker mode enforce egress policy", async () => {
  await ensurePyodideCacheReady();
  const binaryPath = await buildCompiledBinary(process.cwd());
  const auxServer = startAuxHttpServer(8798);
  const policyPath = writePolicy("compiled", denyLoopbackPolicy());
  try {
    for (const [port, workers] of [
      [8799, 0],
      [8800, 2],
    ] as const) {
      const cmd = [binaryPath, "--port", String(port), "--pyodide-cache", CACHE_DIR, "--egress-policy-file", policyPath];
      if (workers > 0) {
        cmd.push("--workers", String(workers));
      }
      await withRuntime(cmd, `http://localhost:${port}`, async () => {
        const result = await executePython(`http://localhost:${port}`, 'import requests; requests.get("http://127.0.0.1:8798/json").status_code');
        expect(result.status).toBe("exception");
        expect(result.result).toContain("Code interpreter egress denied:");
        expect(result.result).toContain("reason=host_denylisted");
      });
    }
  } finally {
    auxServer.stop();
  }
}, 300_000);
