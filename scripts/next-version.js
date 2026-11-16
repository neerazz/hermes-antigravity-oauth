#!/usr/bin/env node
// Pick the version for this release and stamp it into package.json + plugin.yaml.
//   - package.json ahead of npm (a deliberate minor/major bump committed by hand) -> publish it as-is
//   - otherwise -> latest published patch + 1
// Prints the chosen version on stdout. No network writes; `npm view` is read-only.
"use strict";

const fs = require("node:fs");
const { execFileSync } = require("node:child_process");

const parse = (v) => {
  const m = /^(\d+)\.(\d+)\.(\d+)$/.exec(String(v).trim());
  if (!m) throw new Error(`not a plain x.y.z version: ${v}`);
  return m.slice(1).map(Number);
};
const cmp = (a, b) => {
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] - b[i];
  return 0;
};

const pkgPath = "package.json";
const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"));
const local = parse(pkg.version);

let published = null;
const registries = [[pkg.name, "https://registry.npmjs.org"]];
// The GitHub distribution has a scoped name; keep npm's existing name unchanged.
if (process.env.GITHUB_PACKAGE_NAME) {
  registries.push([process.env.GITHUB_PACKAGE_NAME, "https://npm.pkg.github.com"]);
}
for (const [name, registry] of registries) {
  try {
    const out = execFileSync("npm", ["view", name, "version", "--registry", registry], {
      encoding: "utf8", stdio: ["ignore", "pipe", "pipe"],
    });
    const version = parse(out);
    if (!published || cmp(version, published) > 0) published = version;
  } catch (err) {
    const msg = String(err.stderr || err.message);
    if (!/E404|404 Not Found/.test(msg)) throw err; // auth/network errors must fail closed
  }
}

const next = !published || cmp(local, published) > 0
  ? local
  : [published[0], published[1], published[2] + 1];
const version = next.join(".");

pkg.version = version;
fs.writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + "\n");

const yamlPath = "plugin.yaml";
const yaml = fs.readFileSync(yamlPath, "utf8");
if (!/^version: .*$/m.test(yaml)) throw new Error("plugin.yaml has no top-level version: line");
fs.writeFileSync(yamlPath, yaml.replace(/^version: .*$/m, `version: ${version}`));

process.stdout.write(version + "\n");
