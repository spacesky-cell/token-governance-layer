"use strict";

const path = require("path");
const { spawnSync } = require("child_process");

const MIN_PYTHON_VERSION = [3, 10];
const MAX_PYTHON_VERSION_EXCLUSIVE = [3, 15];
const SUPPORTED_PYTHON_RANGE = `${MIN_PYTHON_VERSION[0]}.${MIN_PYTHON_VERSION[1]}`
  + `-${MAX_PYTHON_VERSION_EXCLUSIVE[0]}.${MAX_PYTHON_VERSION_EXCLUSIVE[1] - 1}`;

function isSupportedPythonVersion(major, minor) {
  const atLeastMinimum = major > MIN_PYTHON_VERSION[0]
    || (major === MIN_PYTHON_VERSION[0] && minor >= MIN_PYTHON_VERSION[1]);
  const belowMaximum = major < MAX_PYTHON_VERSION_EXCLUSIVE[0]
    || (
      major === MAX_PYTHON_VERSION_EXCLUSIVE[0]
      && minor < MAX_PYTHON_VERSION_EXCLUSIVE[1]
    );
  return atLeastMinimum && belowMaximum;
}

function runPythonModule(moduleName, args) {
  const packageRoot = path.resolve(__dirname, "..");
  const srcPath = path.join(packageRoot, "src");
  const python = findPython();

  if (!python) {
    console.error(
      `Token Governance Layer requires Python ${SUPPORTED_PYTHON_RANGE} on PATH.`
    );
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
    const minimum = `(${MIN_PYTHON_VERSION.join(", ")})`;
    const maximum = `(${MAX_PYTHON_VERSION_EXCLUSIVE.join(", ")})`;
    const result = spawnSync(
      candidate.command,
      [
        ...candidate.prefixArgs,
        "-c",
        `import sys; raise SystemExit(0 if ${minimum} <= sys.version_info[:2] < ${maximum} else 1)`,
      ],
      { stdio: "ignore", windowsHide: true }
    );
    if (result.status === 0) {
      return candidate;
    }
  }
  return null;
}

module.exports = { isSupportedPythonVersion, runPythonModule };
