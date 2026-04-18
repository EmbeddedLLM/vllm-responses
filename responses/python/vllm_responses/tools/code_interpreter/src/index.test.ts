import { test } from "bun:test";

import { PYODIDE_CACHE_DIR, buildCompiledBinary } from "./test-utils";

test("compiled REPL integration test", async () => {
  // Build the compiled binary
  console.log("Building compiled REPL...");
  const binaryPath = await buildCompiledBinary(process.cwd());
  console.log("Build completed");

  // Test the compiled binary
  console.log("Testing compiled REPL...");

  const replProc = Bun.spawn([binaryPath, "--pyodide-cache", PYODIDE_CACHE_DIR], {
    cwd: process.cwd(),
    stdin: "pipe",
    stdout: "pipe",
    stderr: "pipe",
  });

  let allOutput = "";
  let lastPromptIndex = 0;

  // Start reading output in background
  const reader = replProc.stdout.getReader();
  const decoder = new TextDecoder();

  // Background task to continuously read stdout
  const readTask = (async () => {
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const text = decoder.decode(value, { stream: true });
        allOutput += text;
      }
    } catch (e) {
      // Stream closed, that's ok
    }
  })();

  // Helper to wait for a new prompt
  const waitForPrompt = async (timeoutMs: number = 5000): Promise<string> => {
    const startTime = Date.now();
    const startIndex = lastPromptIndex;

    while (Date.now() - startTime < timeoutMs) {
      const newPromptIndex = allOutput.indexOf(">>> ", lastPromptIndex);
      if (newPromptIndex > lastPromptIndex) {
        const output = allOutput.substring(startIndex, newPromptIndex);
        lastPromptIndex = newPromptIndex + 4; // Skip past ">>> "
        return output;
      }
      await Bun.sleep(100);
    }

    // Return what we have so far
    return allOutput.substring(startIndex);
  };

  // Wait for initial prompt
  await waitForPrompt(60 * 1000); // 1 minute timeout
  console.log("REPL started, sending test commands...");

  // Test 1: 1+1
  replProc.stdin.write("1+1\n");
  await Bun.sleep(100); // Give it a moment to process
  const output1 = await waitForPrompt();
  console.log("Test 1+1 output:", output1);
  if (!output1.includes("2")) {
    throw new Error(`Expected REPL output to contain 2, got: ${output1}`);
  }

  // Test 2: state persists across REPL commands
  replProc.stdin.write("x = 21\n");
  await Bun.sleep(100); // Give it a moment to process
  await waitForPrompt();

  replProc.stdin.write("x * 2\n");
  await Bun.sleep(100);
  const output2 = await waitForPrompt();
  console.log("Test stateful REPL output:", output2);
  if (!output2.includes("42")) {
    throw new Error(`Expected REPL output to contain 42, got: ${output2}`);
  }

  // Cleanup: close the process
  replProc.kill();
  await readTask.catch(() => {}); // Wait for read task to finish

  console.log("Compiled REPL integration test passed!");
}, 180000); // 3 minute timeout for full integration test
