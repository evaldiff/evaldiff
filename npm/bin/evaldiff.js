#!/usr/bin/env node
"use strict";

// evaldiff — CI for LLM prompts. (npm name-reservation stub)
const VERSION = "0.0.20";

const args = process.argv.slice(2);

if (args.length === 0 || args[0] === "--version" || args[0] === "-v") {
  console.log(`evaldiff ${VERSION}`);
  process.exit(0);
}

console.error(`evaldiff ${VERSION} — npm stub. The full CLI is on PyPI: pip install evaldiff`);
console.error(`Gate your prompts: evaldiff run --dataset ds.json --endpoint <url> --model <name>`);
console.error(`Compare runs:       evaldiff diff --base <runA> --new <runB>  (exit 0/1/2)`);
console.error(`Docs & status: https://evaldiff.io`);
process.exit(0);
