"use strict";

// Minimal smoke test for the evaldiff npm stub.
const { execFileSync } = require("node:child_process");
const path = require("node:path");

const BIN = path.join(__dirname, "bin", "evaldiff.js");

function run(args) {
  return execFileSync(process.execPath, [BIN, ...args], {
    encoding: "utf8",
  });
}

const out = run(["--version"]);
if (!/^evaldiff 0\.0\.10$/.test(out.trim())) {
  console.error("FAIL: unexpected version output:", JSON.stringify(out));
  process.exit(1);
}
console.log("PASS: evaldiff --version ->", out.trim());
