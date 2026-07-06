#!/usr/bin/env node
"use strict";

const { runPythonModule } = require("./tgl-python-runner");

runPythonModule("token_governance.cli", process.argv.slice(2));
