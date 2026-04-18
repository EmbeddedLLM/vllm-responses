import { PyodideManager, PythonRuntimeTerminatedError } from "./pyodide-manager";
import { WorkerPool } from "./worker-pool";
import type { ExecuteRequest, ExecutionResult, HealthResponse, HealthStatus, PyodideConfig } from "./types";

let pyodideManager: PyodideManager | null = null;
let workerPool: WorkerPool | null = null;
let serverConfig: ServerConfig | null = null;
let serverStartTime = Date.now();
let server: ReturnType<typeof Bun.serve> | null = null;
let singleThreadRecovery: Promise<void> | null = null;
let recoveryHandlersInstalled = false;

interface ServerConfig {
  port: number;
  resetGlobals: boolean;
  pyodideCache: string;
  workerCount: number;
  egressPolicyFile?: string;
}

export async function startServer(config: ServerConfig) {
  // Store config for use in handleExecute
  serverConfig = config;

  console.log("Initializing execution environment...");

  if (config.workerCount > 0) {
    // Use worker pool
    console.log(`Starting with ${config.workerCount} workers...`);
    workerPool = new WorkerPool({
      workerCount: config.workerCount,
      pyodideConfig: {
        pyodideCache: config.pyodideCache,
        verbose: true,
        timeout: 30000,
        egressPolicyFile: config.egressPolicyFile,
      },
    });
    await workerPool.initialize();
  } else {
    // Use single-threaded PyodideManager (backward compatible)
    console.log("Starting in single-threaded mode...");
    await initializeSingleThreadManager(config);
    setupRuntimeRecoveryHandlers();
  }

  console.log("Execution environment ready");

  // Start HTTP server
  server = Bun.serve({
    port: config.port,
    async fetch(req) {
      const url = new URL(req.url);

      // CORS headers
      const headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
      };

      // Handle OPTIONS preflight
      if (req.method === "OPTIONS") {
        return new Response(null, { headers, status: 204 });
      }

      // GET /health
      if (url.pathname === "/health" && req.method === "GET") {
        return handleHealth(headers);
      }

      // POST /python
      if (url.pathname === "/python" && req.method === "POST") {
        return handleExecute(req, serverConfig!.resetGlobals, headers, serverConfig!);
      }

      // 404
      return new Response("Not Found", { status: 404, headers });
    },
  });

  console.log(`Server listening on http://localhost:${server.port}`);

  // Setup graceful shutdown handlers
  setupShutdownHandlers();
}

async function handleHealth(headers: Record<string, string>): Promise<Response> {
  const health = workerPool ? buildWorkerHealth() : buildSingleThreadHealth();

  return Response.json(health, { headers });
}

async function handleExecute(req: Request, defaultResetGlobals: boolean, headers: Record<string, string>, config: ServerConfig): Promise<Response> {
  try {
    // Parse request
    const body = (await req.json()) as ExecuteRequest;

    if (!body.code || typeof body.code !== "string") {
      return Response.json({ status: "error", error: 'Missing or invalid "code" field' }, { status: 400, headers });
    }

    // Worker-pool mode does not provide stable shared-state semantics because
    // requests are routed independently, so we always force isolated globals
    // when `workerCount > 0` even if the request explicitly asks otherwise.
    const resetGlobals = config.workerCount > 0 ? true : (body.reset_globals ?? defaultResetGlobals);

    // Execute via worker pool or manager
    const result = workerPool ? await workerPool.execute(body.code, resetGlobals) : await executeSingleThread(body.code, resetGlobals);

    // Always return 200 for Python execution (errors are treated as output)
    return Response.json(result, { status: 200, headers });
  } catch (error: any) {
    if (isRuntimeTerminationError(error)) {
      if (!workerPool) {
        void startSingleThreadRecovery(error);
      }
      return buildRuntimeTerminatedResponse(headers);
    }
    return Response.json(
      {
        status: "error",
        error: error.message || "Internal server error",
      },
      { status: 500, headers },
    );
  }
}

function createPyodideConfig(config: ServerConfig): PyodideConfig {
  return {
    pyodideCache: config.pyodideCache,
    verbose: true,
    timeout: 30000,
    egressPolicyFile: config.egressPolicyFile,
  };
}

async function initializeSingleThreadManager(config: ServerConfig): Promise<void> {
  pyodideManager = new PyodideManager(createPyodideConfig(config));
  await pyodideManager.initialize();
}

async function executeSingleThread(code: string, resetGlobals: boolean): Promise<ExecutionResult> {
  if (singleThreadRecovery || !pyodideManager) {
    throw new PythonRuntimeTerminatedError();
  }
  return pyodideManager.execute(code, resetGlobals);
}

function buildRuntimeTerminatedResponse(headers: Record<string, string>): Response {
  return Response.json(
    {
      status: "error",
      error: "Python runtime terminated",
    },
    { status: 500, headers },
  );
}

function isRuntimeTerminationError(error: unknown): boolean {
  if (error instanceof PythonRuntimeTerminatedError) {
    return true;
  }
  const e = error as {
    name?: unknown;
    message?: unknown;
    stack?: unknown;
  };
  const combined = [e?.name, e?.message, e?.stack].map((value) => String(value ?? "")).join("\n");
  return combined.includes("PythonRuntimeTerminatedError") || combined.includes("Python runtime terminated") || combined.includes("SystemExit");
}

function looksLikePyodideRuntimeEscape(error: unknown): boolean {
  const e = error as {
    message?: unknown;
    stack?: unknown;
  };
  const combined = [e?.message, e?.stack].map((value) => String(value ?? "")).join("\n");
  return combined.includes("SystemExit") || combined.includes("Python runtime terminated");
}

function setupRuntimeRecoveryHandlers(): void {
  if (recoveryHandlersInstalled) {
    return;
  }
  recoveryHandlersInstalled = true;

  const handleEscape = (kind: "uncaughtException" | "unhandledRejection", error: unknown) => {
    if (serverConfig?.workerCount === 0 && looksLikePyodideRuntimeEscape(error)) {
      console.error(`Recovered ${kind} from poisoned Pyodide runtime; recycling runtime...`, error);
      void startSingleThreadRecovery(error);
      return;
    }

    console.error(`Unhandled ${kind}:`, error);
    process.exit(1);
  };

  process.on("uncaughtException", (error) => {
    handleEscape("uncaughtException", error);
  });
  process.on("unhandledRejection", (reason) => {
    handleEscape("unhandledRejection", reason);
  });
}

async function startSingleThreadRecovery(reason: unknown): Promise<void> {
  if (!serverConfig || singleThreadRecovery) {
    return singleThreadRecovery ?? Promise.resolve();
  }

  console.error("Single-threaded Pyodide runtime terminated; starting recovery...", reason);
  pyodideManager = null;

  singleThreadRecovery = (async () => {
    const manager = new PyodideManager(createPyodideConfig(serverConfig!));
    await manager.initialize();
    pyodideManager = manager;
    console.log("Single-threaded Pyodide runtime recovered");
  })()
    .catch((error) => {
      console.error("Single-threaded Pyodide runtime recovery failed:", error);
      pyodideManager = null;
    })
    .finally(() => {
      singleThreadRecovery = null;
    });

  return singleThreadRecovery;
}

function buildSingleThreadHealth(): HealthResponse {
  const status: HealthStatus = singleThreadRecovery ? "unhealthy" : pyodideManager ? "healthy" : "unhealthy";
  return {
    status,
    pyodide_loaded: status === "healthy",
    uptime_seconds: Math.floor((Date.now() - serverStartTime) / 1000),
    execution_count: pyodideManager?.getExecutionCount() || 0,
  };
}

function buildWorkerHealth(): HealthResponse {
  const readyWorkerCount = workerPool?.getReadyWorkerCount() || 0;
  const configuredWorkerCount = workerPool?.getConfiguredWorkerCount() || serverConfig?.workerCount || 0;
  const status: HealthStatus = readyWorkerCount === 0 ? "unhealthy" : readyWorkerCount === configuredWorkerCount ? "healthy" : "degraded";
  return {
    status,
    pyodide_loaded: readyWorkerCount > 0,
    uptime_seconds: Math.floor((Date.now() - serverStartTime) / 1000),
    execution_count: workerPool?.getExecutionCount() || 0,
    ready_worker_count: readyWorkerCount,
    configured_worker_count: configuredWorkerCount,
  };
}

function setupShutdownHandlers() {
  let isShuttingDown = false;

  const shutdown = async (signal: string) => {
    if (isShuttingDown) {
      return;
    }
    isShuttingDown = true;

    console.log(`\nReceived ${signal}, shutting down gracefully...`);

    try {
      // Stop accepting new requests
      if (server) {
        server.stop();
      }

      // Terminate worker pool if running
      if (workerPool) {
        console.log("Terminating worker pool...");
        workerPool.terminate();
      }

      // Cleanup pyodide manager
      if (pyodideManager) {
        console.log("Cleaning up Pyodide manager...");
        // PyodideManager doesn't have a cleanup method, but it will be GC'd
      }

      console.log("Server shutdown complete");
      process.exit(0);
    } catch (error) {
      console.error("Error during shutdown:", error);
      process.exit(1);
    }
  };

  // Handle SIGINT (Ctrl+C)
  process.on("SIGINT", () => shutdown("SIGINT"));

  // Handle SIGTERM (e.g., kill command)
  process.on("SIGTERM", () => shutdown("SIGTERM"));
}
