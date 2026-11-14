#!/usr/bin/env node
// npx hermes-antigravity-oauth [install|login|status|uninstall]
// Thin wrapper over the official CLIs: `hermes plugins install`, `hermes auth`, and Google's agy.
// No token ever passes through this script.
"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const REPO = "https://github.com/neerazz/hermes-antigravity-oauth";
const PLUGIN = "antigravity-oauth";
// Google's official agy installer. Downloaded to a temp file and run from disk, never piped.
const AGY_INSTALLER = process.platform === "win32"
  ? "https://antigravity.google/cli/install.ps1"
  : "https://antigravity.google/cli/install.sh";
const HERMES_DOCS = "https://hermes-agent.nousresearch.com/docs";

const [, , cmd = "install", ...rest] = process.argv;
const flags = new Set(rest);
const yes = flags.has("--yes") || flags.has("-y");

function which(bin) {
  const r = spawnSync(process.platform === "win32" ? "where" : "which", [bin], { encoding: "utf8" });
  return r.status === 0 ? r.stdout.split(/\r?\n/)[0].trim() : null;
}

function run(bin, args, opts = {}) {
  console.log(`$ ${bin} ${args.join(" ")}`);
  const r = spawnSync(bin, args, { stdio: "inherit", shell: process.platform === "win32", ...opts });
  if (r.error) throw r.error;
  return r.status ?? 1;
}

function die(msg, code = 1) {
  console.error(`\n✗ ${msg}`);
  process.exit(code);
}

function requireHermes() {
  if (!which("hermes")) {
    die(`Hermes Agent not found on PATH. Install it first: ${HERMES_DOCS}`);
  }
}

async function ensureAgy() {
  if (which("agy")) return;
  if (!yes) {
    die(`Antigravity CLI (agy) not found.\n  Install it from https://antigravity.google/cli\n  or re-run with --yes to install it now.`);
  }
  console.log(`Downloading the official Antigravity installer: ${AGY_INSTALLER}`);
  const res = await fetch(AGY_INSTALLER);
  if (!res.ok) die(`Download failed: HTTP ${res.status}`);
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "agy-install-")), path.basename(AGY_INSTALLER));
  fs.writeFileSync(file, Buffer.from(await res.arrayBuffer()), { mode: 0o700 });
  const code = process.platform === "win32"
    ? run("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", file])
    : run("bash", [file]);
  fs.rmSync(path.dirname(file), { recursive: true, force: true });
  if (code !== 0 || !which("agy")) {
    die("agy did not land on PATH. Open a new shell (or add ~/.local/bin to PATH) and re-run.");
  }
}

async function install() {
  requireHermes();
  await ensureAgy();
  const ref = rest.find((a) => /^[0-9a-f]{40}$/.test(a));
  const args = ["plugins", "install", REPO, "--enable"];
  if (ref) args.push("--ref", ref);
  if (flags.has("--force")) args.push("--force");
  if (run("hermes", args) !== 0) die("`hermes plugins install` failed (see output above).");
  if (flags.has("--no-login")) {
    console.log(`\nInstalled. Sign in later with: hermes auth add ${PLUGIN}`);
    return;
  }
  await login();
}

async function login() {
  requireHermes();
  await ensureAgy();
  const code = run("hermes", ["auth", "add", PLUGIN]);
  if (code !== 0) die("Sign-in did not complete.", code);
  // No hardcoded model: agy's catalog depends on the account and changes over time.
  console.log(`\n✓ Done. Pick an exact model ID with \`agy models\`, then run:\n  hermes --provider ${PLUGIN} -m '<exact-model-id>'`);
}

function status() {
  requireHermes();
  process.exit(run("hermes", ["auth", "status", PLUGIN]));
}

function uninstall() {
  requireHermes();
  process.exit(run("hermes", ["plugins", "remove", PLUGIN]));
}

function help() {
  console.log(`hermes-antigravity-oauth — Google Antigravity sign-in for Hermes Agent

Usage: npx hermes-antigravity-oauth [command] [flags]

Commands:
  install     Install + enable the Hermes plugin, then sign in (default)
  login       Sign in only (runs \`hermes auth add ${PLUGIN}\`)
  status      Show sign-in state
  uninstall   Remove the plugin (your agy session is left alone)

Flags:
  --yes, -y      Install the agy CLI automatically if missing
  --no-login     Install without signing in
  --force        Reinstall over an existing copy
  <40-hex sha>   Pin the plugin to an exact commit

Platform: ${os.platform()} ${os.arch()}`);
}

const commands = { install, login, status, uninstall, help, "--help": help, "-h": help };
Promise.resolve((commands[cmd] || (() => { help(); process.exit(2); }))()).catch((err) => die(err.message || String(err)));
