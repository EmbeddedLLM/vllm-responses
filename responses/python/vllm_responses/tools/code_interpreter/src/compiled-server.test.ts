import type { Subprocess } from "bun";
import { test } from "bun:test";

import { PYODIDE_CACHE_DIR, assertStartupAndExecution, buildCompiledBinary } from "./test-utils";

interface RuntimeShape {
  name: string;
  port: number;
  workers: number;
}

const COMPILED_RUNTIME_SHAPES: RuntimeShape[] = [
  { name: "compiled server workers=0", port: 8775, workers: 0 },
  { name: "compiled server workers=2", port: 8776, workers: 2 },
];

for (const shape of COMPILED_RUNTIME_SHAPES) {
  test(`startup + execution smoke: ${shape.name}`, async () => {
    let serverProc: Subprocess | null = null;
    const serverUrl = `http://localhost:${shape.port}`;
    const binaryPath = await buildCompiledBinary(process.cwd());

    try {
      const cmd = [binaryPath, "--port", String(shape.port), "--pyodide-cache", PYODIDE_CACHE_DIR];
      if (shape.workers > 0) {
        cmd.push("--workers", String(shape.workers));
      }

      serverProc = Bun.spawn(cmd, {
        cwd: process.cwd(),
        stdout: "inherit",
        stderr: "inherit",
      });

      await assertStartupAndExecution(serverUrl);
    } finally {
      if (serverProc) {
        serverProc.kill();
        await serverProc.exited;
      }
    }
  }, 180000);
}
