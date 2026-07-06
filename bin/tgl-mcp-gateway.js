#!/usr/bin/env node
"use strict";

const { runPythonModule } = require("./tgl-python-runner");

runPythonModule("token_governance.mcp_gateway", process.argv.slice(2));
