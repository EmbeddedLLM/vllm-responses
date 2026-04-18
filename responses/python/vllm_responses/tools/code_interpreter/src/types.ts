export interface PyodideConfig {
  pyodideCache: string;
  verbose?: boolean;
  timeout?: number;
  egressPolicyFile?: string;
}

export type HealthStatus = "healthy" | "degraded" | "unhealthy";

export interface ExecutionResult {
  status: "success" | "exception" | "error";
  /**
   * Captured stdout from the Python execution (e.g. `print(...)` output).
   *
   * Note: This is the low-level Python stdout stream; the gateway may map this into
   * OpenAI Responses `code_interpreter_call.outputs` depending on `include[]`.
   */
  stdout: string;
  /**
   * Captured stderr from the Python execution.
   */
  stderr: string;
  result: string | null;
  error?: string;
  execution_time_ms: number;
}

export interface ExecuteRequest {
  code: string;
  reset_globals?: boolean;
}

export interface HealthResponse {
  status: HealthStatus;
  pyodide_loaded: boolean;
  uptime_seconds: number;
  execution_count: number;
  ready_worker_count?: number;
  configured_worker_count?: number;
}

// Worker message types
export interface WorkerRequest {
  type: "execute";
  id: string;
  code: string;
  resetGlobals: boolean;
}

export interface WorkerResponse {
  type: "result" | "error" | "ready";
  workerId?: number;
  id?: string;
  result?: ExecutionResult;
  error?: string;
  fatal?: boolean;
}

export interface WorkerInitMessage {
  type: "init";
  config: PyodideConfig;
  workerId: number;
}
