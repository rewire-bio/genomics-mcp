#!/usr/bin/env node
// MCPB launcher: runs the digest-pinned genomics-mcp container over stdio.
// Usage: node launcher.cjs <docker executable> <read-only data dir> <writable work dir>
// Only the two chosen directories are mounted. No home directory, no cloud credentials.
"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

// Replaced by scripts/build_mcpb.py with ghcr.io/rewire-bio/genomics-mcp@sha256:<digest>.
const IMAGE = "@IMAGE@";
const PINNED = /^ghcr\.io\/rewire-bio\/genomics-mcp@sha256:[0-9a-f]{64}$/;

function fail(message) {
  process.stderr.write(`genomics-mcp launcher: ${message}\n`);
  process.exit(2);
}

function realDir(label, value) {
  if (!value || !path.isAbsolute(value)) fail(`${label} must be an absolute path`);
  let real;
  try {
    real = fs.realpathSync(value);
  } catch {
    fail(`${label} does not exist: ${value}`);
  }
  if (!fs.statSync(real).isDirectory()) fail(`${label} is not a directory: ${value}`);
  // Docker --mount values are comma separated; refuse anything that could change the syntax.
  if (/[,\n\r"]/.test(real)) fail(`${label} contains an unsupported character (, newline or ")`);
  const home = fs.realpathSync(os.homedir());
  if (real === path.parse(real).root) fail(`${label} may not be the filesystem root`);
  if (real === home || home.startsWith(real + path.sep)) {
    fail(`${label} may not be your home directory or contain it; choose a dedicated directory`);
  }
  return real;
}

function inside(child, parent) {
  return child === parent || child.startsWith(parent + path.sep);
}

function main(argv) {
  if (!PINNED.test(IMAGE) && process.env.GENOMICS_MCP_LAUNCHER_TEST_IMAGE === undefined) {
    fail("bundle was built without a digest-pinned image");
  }
  const image = PINNED.test(IMAGE) ? IMAGE : process.env.GENOMICS_MCP_LAUNCHER_TEST_IMAGE;
  const [docker, dataArg, workArg] = argv;
  if (!docker || !path.isAbsolute(docker)) fail("Docker executable must be an absolute path");
  try {
    fs.accessSync(docker, fs.constants.X_OK);
  } catch {
    fail(`Docker executable is not executable: ${docker}`);
  }
  const data = realDir("data directory", dataArg);
  const work = realDir("workspace directory", workArg);
  if (inside(data, work) || inside(work, data)) {
    fail("data and workspace directories must be separate, not nested");
  }

  const args = [
    "run", "--rm", "-i",
    "--platform", "linux/amd64",
    "--read-only",
    "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--mount", `type=bind,source=${data},target=/data,readonly`,
    "--mount", `type=bind,source=${work},target=/work`,
    "-e", "GENOMICS_MCP_ALLOWED_ROOTS=/data",
    "-e", "GENOMICS_MCP_WORK_DIR=/work",
    "-e", "AWS_EC2_METADATA_DISABLED=true",
    "-e", "AWS_CONFIG_FILE=/dev/null",
    "-e", "AWS_SHARED_CREDENTIALS_FILE=/dev/null",
  ];
  // On Linux the bind-mounted workspace belongs to the host user; run as that user.
  if (process.platform === "linux" && typeof process.getuid === "function") {
    args.push("--user", `${process.getuid()}:${process.getgid()}`);
  }
  args.push(image, "--transport", "stdio");

  // The Docker CLI gets a minimal environment: nothing from AWS_*, proxies or HTSlib.
  const env = {};
  for (const key of ["PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "SYSTEMROOT"]) {
    if (process.env[key] !== undefined) env[key] = process.env[key];
  }
  const child = spawn(docker, args, { stdio: "inherit", env, shell: false });
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    process.on(signal, () => child.kill(signal));
  }
  child.on("error", (err) => fail(`could not start Docker: ${err.message}`));
  child.on("exit", (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
}

main(process.argv.slice(2));
