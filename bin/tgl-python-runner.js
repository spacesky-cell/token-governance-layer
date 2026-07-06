"use strict";

const path = require("path");
const { spawnSync } = require("child_process");

function runPythonModule(moduleName, args) {
  const packageRoot = path.resolve(__dirname, "..");
  const srcPath = path.join(packageRoot, "src");
  const python = findPython();

  if (!python) {
    console.error("Token Governance Layer requires Python 3.10+ on PATH.");
    console.error("Install Python, then run this command again.");
    process.exit(127);
  }

  const env = { ...process.env };
  env.PYTHONPATH = env.PYTHONPATH ? `${srcPath}${path.delimiter}${env.PYTHONPATH}` : srcPath;
  env.TGL_NPM_WRAPPER = "1";

  const result = spawnSync(python.command, [...python.prefixArgs, "-m", moduleName, ...args], {
    stdio: "inherit",
    env,
    windowsHide: true
  });

  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }

  process.exit(typeof result.status === "number" ? result.status : 1);
}

function findPython() {
  const candidates = process.platform === "win32"
    ? [
        { command: "py", prefixArgs: ["-3"] },
        { command: "python", prefixArgs: [] },
        { command: "python3", prefixArgs: [] }
      ]
    : [
        { command: "python3", prefixArgs: [] },
        { command: "python", prefixArgs: [] }
      ];

  for (const candidate of candidates) {
    const result = spawnSync(
      candidate.command,
      [...candidate.prefixArgs, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"],
      { stdio: "ignore", windowsHide: true }
    );
    if (result.status === 0) {
      return candidate;
    }
  }
  return null;
}

module.exports = { runPythonModule };
