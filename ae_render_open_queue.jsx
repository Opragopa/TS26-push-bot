#!/usr/bin/env node
/* Compatibility launcher for hosts that accidentally picked this file as the app entrypoint. */
const { spawn } = require("node:child_process");

const python = process.env.PYTHON || process.env.PYTHON_BIN || "python3";
const child = spawn(python, ["main.py", ...process.argv.slice(2)], {
  cwd: __dirname,
  env: process.env,
  stdio: "inherit",
});

child.on("error", (error) => {
  console.error(`Failed to start python main.py: ${error.message}`);
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 0);
});
