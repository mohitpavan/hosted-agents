#!/usr/bin/env node

import { mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

function fail(error, code, details = "") {
  process.stdout.write(JSON.stringify({ success: false, error, code, details }) + "\n");
  process.exit(1);
}

function parseArgs(args) {
  const options = {};
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === "--help" || arg === "-h") {
      options.help = true;
    } else if (arg === "--endpoint") {
      options.endpoint = args[++i];
    } else if (arg === "--token") {
      options.token = args[++i];
    } else if (arg === "--session") {
      options.session = args[++i];
    } else if (arg === "--os") {
      options.osName = args[++i];
    } else if (arg === "--output") {
      options.outputDir = args[++i];
    } else if (arg === "--artifact-base") {
      options.artifactBaseDir = args[++i];
    } else {
      fail(`Unknown option: ${arg}`, "invalid_args");
    }
  }
  return options;
}

function usage() {
  console.log(`Usage: node scripts/remote/connect.mjs [options]

Options:
  --endpoint <url>    Azure Playwright Service WSS endpoint
  --token <token>     Azure Playwright Service access token
  --session <name>    Session name (default: default)
  --os <linux|windows> Remote browser OS (default: linux)
  --output <dir>      Config base directory (default: .playwright-remote)
  --artifact-base <dir> Artifact base directory (default: BROWSER_ARTIFACT_BASE_DIR or .)`);
}

function resolveArtifactBaseDir(value) {
  const artifactBase = value || process.env.BROWSER_ARTIFACT_BASE_DIR || ".";
  if (artifactBase === "~") {
    return homedir();
  }
  if (artifactBase.startsWith("~/") || artifactBase.startsWith("~\\")) {
    return resolve(homedir(), artifactBase.slice(2));
  }
  return resolve(artifactBase);
}

const options = parseArgs(process.argv.slice(2));
if (options.help) {
  usage();
  process.exit(0);
}

const endpoint = options.endpoint || process.env.PLAYWRIGHT_SERVICE_URL || "";
const token = options.token || process.env.PLAYWRIGHT_SERVICE_ACCESS_TOKEN || "";
const missing = [];
if (!endpoint) missing.push("endpoint");
if (!token) missing.push("token");
if (missing.length) {
  fail("Playwright Service endpoint and access token are required.", "missing_credentials", missing.join(","));
}

const session = options.session || "default";
const osName = options.osName || "linux";
const baseDir = resolve(options.outputDir || ".playwright-remote");
const sessionDir = join(baseDir, session);
const configPath = join(sessionDir, "config.json");
const artifactBaseDir = resolveArtifactBaseDir(options.artifactBaseDir);
const artifactDir = join(artifactBaseDir, "artifacts", session);
let remoteEndpoint;
try {
  const url = new URL(endpoint);
  url.searchParams.set("os", osName);
  url.searchParams.set("browser", "chromium");
  url.searchParams.set("playwrightVersion", "cdp");
  url.searchParams.set("accessKey", token);
  remoteEndpoint = url.toString();
} catch (error) {
  fail(`Invalid Playwright Service endpoint: ${error.message}`, "invalid_endpoint");
}

const config = {
  browser: {
    browserName: "chromium",
    isolated: true,
    remoteEndpoint
  },
  outputDir: artifactDir,
  outputMode: "file",
  timeouts: {
    action: 15000,
    navigation: 90000
  }
};

try {
  mkdirSync(sessionDir, { recursive: true });
  mkdirSync(artifactDir, { recursive: true });
  writeFileSync(configPath, JSON.stringify(config, null, 2) + "\n", "utf-8");
} catch (error) {
  fail(`Failed to write playwright-cli config: ${error.message}`, "file_write_error", configPath);
}

process.stdout.write(JSON.stringify({
  success: true,
  session,
  configPath,
  configFlag: `--config ${configPath}`,
  artifactDir,
  cleanup: `node scripts/remote/disconnect.mjs --session ${session}`
}) + "\n");

