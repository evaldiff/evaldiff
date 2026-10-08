#!/usr/bin/env node
"use strict";

// evaldiff — CI for LLM prompts. (v0.0.1: name reservation stub)
const VERSION = "0.0.11";

const args = process.argv.slice(2);

if (args.length === 0 || args[0] === "--version" || args[0] === "-v") {
  console.log(`evaldiff ${VERSION}`);
  process.exit(0);
}

console.error(`evaldiff ${VERSION} — npm stub. The full CLI is on PyPI: pip install evaldiff`);
console.error(`Docs & status: https://evaldiff.io`);
process.exit(0);
