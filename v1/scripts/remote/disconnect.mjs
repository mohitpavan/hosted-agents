#!/usr/bin/env node

import { existsSync, readdirSync, rmSync } from "node:fs";
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
    } else if (arg === "--session") {
      options.session = args[++i];
    } else if (arg === "--all") {
      options.all = true;
    } else {
      fail(`Unknown option: ${arg}`, "invalid_args");
    }
  }
  return options;
}

function usage() {
  console.log(`Usage: node scripts/remote/disconnect.mjs [--session <name>] [--all]

Deletes .playwright-remote session config files that contain embedded remote browser tokens.`);
}

function cleanSession(baseDir, session) {
  const sessionDir = join(baseDir, session);
  const cleaned = [];
  if (!existsSync(sessionDir)) {
    return cleaned;
  }
  const configPath = join(sessionDir, "config.json");
  if (existsSync(configPath)) {
    rmSync(configPath, { force: true });
    cleaned.push(configPath);
  }
  try {
    const remaining = readdirSync(sessionDir);
    if (remaining.length === 0) {
      rmSync(sessionDir, { recursive: true, force: true });
    }
  } catch {
    // Non-fatal cleanup of the empty session directory.
  }
  return cleaned;
}

const options = parseArgs(process.argv.slice(2));
if (options.help) {
  usage();
  process.exit(0);
}

const baseDir = resolve(".playwright-remote");
const cleaned = [];

try {
  if (options.all) {
    if (existsSync(baseDir)) {
      for (const entry of readdirSync(baseDir)) {
        cleaned.push(...cleanSession(baseDir, entry));
      }
      if (existsSync(baseDir) && readdirSync(baseDir).length === 0) {
        rmSync(baseDir, { recursive: true, force: true });
      }
    }
  } else {
    cleaned.push(...cleanSession(baseDir, options.session || "default"));
  }
} catch (error) {
  fail(`Failed to clean remote browser config: ${error.message}`, "cleanup_error");
}

process.stdout.write(JSON.stringify({ success: true, cleaned }) + "\n");

