#!/usr/bin/env node
"use strict";

const { runPythonModule } = require("./tgl-python-runner");

runPythonModule("token_governance.claude_hook", process.argv.slice(2));
