import type { ExecutionResult, PyodideConfig } from "./types";

interface WorkerPoolConfig {
  workerCount: number;
  pyodideConfig: PyodideConfig;
}

interface WorkerState {
  worker: Worker;
  ready: boolean;
  workerId: number;
  generation: number;
  terminating: boolean;
}

interface PendingRequest {
  resolve: (result: ExecutionResult) => void;
  reject: (error: Error) => void;
  timeout: Timer;
  workerId: number;
  generation: number;
}

interface WorkerMessage {
  type: "result" | "error" | "ready";
  workerId?: number;
  id?: string;
  result?: ExecutionResult;
  error?: string;
  fatal?: boolean;
}

const PYTHON_RUNTIME_TERMINATED_MESSAGE = "Python runtime terminated";

function normalizeWorkerFailureReason(reason: string): string {
  return reason.includes(PYTHON_RUNTIME_TERMINATED_MESSAGE) || reason.includes("SystemExit") ? PYTHON_RUNTIME_TERMINATED_MESSAGE : reason;
}

export class WorkerPool {
  private workers = new Map<number, WorkerState>();
  private pendingRequests = new Map<string, PendingRequest>();
  private config: WorkerPoolConfig;
  private nextRequestId = 0;
  private executionCount = 0;
  private terminating = false;

  constructor(config: WorkerPoolConfig) {
    this.config = config;
  }

  async initialize(): Promise<void> {
    console.log(`Initializing ${this.config.workerCount} workers...`);
    await Promise.all(Array.from({ length: this.config.workerCount }, (_, workerId) => this.createWorker(workerId)));
    console.log(`All ${this.config.workerCount} workers ready`);
  }

  private createWorker(workerId: number): Promise<void> {
    const existing = this.workers.get(workerId);
    const generation = (existing?.generation ?? 0) + 1;

    return new Promise((resolve, reject) => {
      if (this.terminating) {
        reject(new Error("WorkerPool terminated"));
        return;
      }

      const worker = new Worker("./worker.ts");
      const state: WorkerState = {
        worker,
        ready: false,
        workerId,
        generation,
        terminating: false,
      };
      this.workers.set(workerId, state);

      let initSettled = false;

      const failInitOrRespawn = (reason: string) => {
        if (!initSettled) {
          initSettled = true;
          reject(new Error(reason));
          return;
        }
        void this.handleWorkerFailure(workerId, generation, reason);
      };

      const readyListener = (event: MessageEvent) => {
        const message = event.data as WorkerMessage;
        if (message.workerId !== workerId || !this.isCurrentGeneration(workerId, generation)) {
          return;
        }
        if (message.type === "ready") {
          state.ready = true;
          worker.removeEventListener("message", readyListener);
          if (!initSettled) {
            initSettled = true;
            resolve();
          }
        } else if (message.type === "error" && !message.id) {
          failInitOrRespawn(message.error || `Worker ${workerId} initialization failed`);
        }
      };

      worker.addEventListener("message", readyListener);
      worker.addEventListener("message", (event: MessageEvent) => {
        this.handleWorkerMessage(workerId, generation, event);
      });
      worker.addEventListener("error", (event: ErrorEvent) => {
        const reason = event.message || `Worker ${workerId} failed`;
        failInitOrRespawn(reason);
      });

      worker.postMessage({
        type: "init",
        config: this.config.pyodideConfig,
        workerId,
      });
    });
  }

  private isCurrentGeneration(workerId: number, generation: number): boolean {
    const state = this.workers.get(workerId);
    return state !== undefined && state.generation === generation;
  }

  private handleWorkerMessage(workerId: number, generation: number, event: MessageEvent): void {
    const message = event.data as WorkerMessage;
    if (!this.isCurrentGeneration(workerId, generation)) {
      return;
    }

    if (message.type === "result" && message.id) {
      const pending = this.pendingRequests.get(message.id);
      if (pending) {
        clearTimeout(pending.timeout);
        this.pendingRequests.delete(message.id);
        pending.resolve(message.result!);
      }
      return;
    }

    if (message.type === "error" && message.id) {
      const pending = this.pendingRequests.get(message.id);
      if (pending) {
        clearTimeout(pending.timeout);
        this.pendingRequests.delete(message.id);
        pending.reject(new Error(message.error || "Worker execution failed"));
      }

      if (message.fatal) {
        void this.handleWorkerFailure(workerId, generation, message.error || PYTHON_RUNTIME_TERMINATED_MESSAGE);
      }
    }
  }

  private async handleWorkerFailure(workerId: number, generation: number, reason: string): Promise<void> {
    const state = this.workers.get(workerId);
    if (!state || state.generation !== generation || state.terminating) {
      return;
    }

    state.ready = false;
    state.terminating = true;
    this.rejectPendingRequestsForWorker(workerId, generation, reason);

    try {
      state.worker.terminate();
    } catch {
      // Ignore termination races for already-dead workers.
    }

    if (this.terminating) {
      this.workers.delete(workerId);
      return;
    }

    console.warn(`Worker ${workerId} failed; respawning clean runtime...`);
    try {
      await this.createWorker(workerId);
      console.log(`Worker ${workerId} recovered`);
    } catch (error) {
      console.error(`Worker ${workerId} respawn failed:`, error);
    }
  }

  private rejectPendingRequestsForWorker(workerId: number, generation: number, reason: string): void {
    const errorMessage = normalizeWorkerFailureReason(reason);

    for (const [requestId, pending] of this.pendingRequests) {
      if (pending.workerId !== workerId || pending.generation !== generation) {
        continue;
      }
      clearTimeout(pending.timeout);
      this.pendingRequests.delete(requestId);
      pending.reject(new Error(errorMessage));
    }
  }

  execute(code: string, resetGlobals: boolean): Promise<ExecutionResult> {
    return new Promise((resolve, reject) => {
      const requestId = `req-${this.nextRequestId++}`;
      const state = this.selectWorkerRandom();

      if (!state) {
        reject(new Error("No workers available"));
        return;
      }

      const timeout = setTimeout(() => {
        const pending = this.pendingRequests.get(requestId);
        if (!pending) {
          return;
        }
        this.pendingRequests.delete(requestId);
        pending.reject(new Error("Request timeout"));
        void this.handleWorkerFailure(state.workerId, state.generation, "Worker request timeout");
      }, this.config.pyodideConfig.timeout || 30000);

      this.pendingRequests.set(requestId, {
        resolve,
        reject,
        timeout,
        workerId: state.workerId,
        generation: state.generation,
      });

      state.worker.postMessage({
        type: "execute",
        id: requestId,
        code,
        resetGlobals,
      });

      this.executionCount++;
    });
  }

  private selectWorkerRandom(): WorkerState | null {
    const readyWorkers = Array.from(this.workers.values()).filter((state) => state.ready && !state.terminating);
    if (readyWorkers.length === 0) {
      return null;
    }

    const randomIndex = Math.floor(Math.random() * readyWorkers.length);
    return readyWorkers[randomIndex] ?? null;
  }

  pyodideLoaded(): boolean {
    return this.getReadyWorkerCount() > 0;
  }

  getExecutionCount(): number {
    return this.executionCount;
  }

  getReadyWorkerCount(): number {
    return Array.from(this.workers.values()).filter((state) => state.ready && !state.terminating).length;
  }

  getConfiguredWorkerCount(): number {
    return this.config.workerCount;
  }

  terminate(): void {
    this.terminating = true;

    for (const state of this.workers.values()) {
      state.terminating = true;
      state.worker.terminate();
    }
    this.workers.clear();

    for (const [id, pending] of this.pendingRequests) {
      clearTimeout(pending.timeout);
      pending.reject(new Error("WorkerPool terminated"));
      this.pendingRequests.delete(id);
    }
  }
}
