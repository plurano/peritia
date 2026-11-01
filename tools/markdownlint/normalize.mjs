// SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
//
// SPDX-License-Identifier: EUPL-1.2

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const MAX_FILES = 200;
const MAX_FILE_BYTES_CEILING = 4_194_304;
const MAX_CAPTURE_BYTES = 1_048_576;
const MAX_MANIFEST_ALIASES = 1_024;
const MAX_MANIFEST_DEPTH = 64;
const MAX_FIX_PASSES = 4;
const INSTALL_TIMEOUT_MS = 300_000;
const NPM_CI_ARGS = Object.freeze(["ci", "--ignore-scripts", "--no-audit", "--no-fund"]);
const TOOL_FILES = Object.freeze(["package.json", "package-lock.json"]);
const MARKDOWNLINT_ROOT_ENTRY = "./lib/exports.mjs";
const MARKDOWNLINT_SYNC_ENTRY = "./lib/exports-sync.mjs";
const NORMALIZE_CONFIG = Object.freeze({
  default: false,
  MD022: true,
  MD031: true,
  MD032: true,
  MD047: true,
  MD060: true,
  MD020: true,
});
const COMPACT_TABLE_CONFIG = Object.freeze({
  default: false,
  MD060: { style: "compact" },
});

function fail(message) {
  throw new Error(message);
}

function command(commandName, args, options = {}) {
  const result = spawnSync(commandName, args, {
    cwd: options.cwd,
    encoding: "utf8",
    env: { ...process.env, ...options.env },
    maxBuffer: MAX_CAPTURE_BYTES,
    stdio: "pipe",
    timeout: options.timeout ?? INSTALL_TIMEOUT_MS,
    windowsHide: true,
  });
  if (result.error) {
    fail(`${commandName} failed: ${result.error.message}`);
  }
  if (result.signal) {
    fail(`${commandName} terminated by ${result.signal}`);
  }
  if (result.status !== 0) {
    const detail = (result.stderr || result.stdout || "").trim().slice(0, 500);
    fail(`${commandName} exited ${result.status}${detail ? `: ${detail}` : ""}`);
  }
  return result;
}

function npmInvocation(platform, execPath, args) {
  if (platform !== "win32") {
    return { file: "npm", args: [...args] };
  }
  const cli = path.resolve(path.dirname(execPath), "node_modules", "npm", "bin", "npm-cli.js");
  if (fs.statSync(cli, { throwIfNoEntry: false })?.isFile() !== true) {
    fail(`npm CLI not found beside ${execPath} at ${cli}`);
  }
  return { file: execPath, args: [cli, ...args] };
}

function install(toolDir, temporary) {
  for (let index = 0; index < TOOL_FILES.length; index += 1) {
    fs.copyFileSync(path.join(toolDir, TOOL_FILES[index]), path.join(temporary, TOOL_FILES[index]));
  }
  const npm = npmInvocation(process.platform, process.execPath, NPM_CI_ARGS);
  command(npm.file, npm.args, { cwd: temporary, timeout: INSTALL_TIMEOUT_MS });
}

function loadDependency(temporary, name) {
  return createRequire(path.join(temporary, "package.json"))(name);
}

function markdownlintEntries(temporary) {
  const realTemp = fs.realpathSync(path.resolve(temporary));
  const lockPath = path.resolve(realTemp, "package-lock.json");
  const lock = JSON.parse(fs.readFileSync(lockPath, "utf8"));
  const locked = lock?.packages?.["node_modules/markdownlint"]?.version;
  if (typeof locked !== "string" || locked === "") {
    fail("package-lock.json pins no markdownlint version");
  }
  const packageDir = fs.realpathSync(path.resolve(realTemp, "node_modules", "markdownlint"));
  const metadataPath = path.resolve(packageDir, "package.json");
  const metadata = JSON.parse(fs.readFileSync(metadataPath, "utf8"));
  if (
    metadata?.version !== locked ||
    metadata?.exports?.["."] !== MARKDOWNLINT_ROOT_ENTRY ||
    metadata?.exports?.["./sync"] !== MARKDOWNLINT_SYNC_ENTRY
  ) {
    fail(`installed markdownlint package does not match locked ${locked} library contract`);
  }
  return {
    rootEntry: path.join(packageDir, MARKDOWNLINT_ROOT_ENTRY),
    syncEntry: path.join(packageDir, MARKDOWNLINT_SYNC_ENTRY),
  };
}

function configurationParsers(temporary) {
  const yaml = loadDependency(temporary, "js-yaml");
  const jsonc = loadDependency(temporary, "jsonc-parser");
  const toml = loadDependency(temporary, "smol-toml");
  const parseJSONC = (text) => {
    const errors = [];
    const result = jsonc.parse(text, errors, { allowTrailingComma: true });
    if (errors.length > 0) {
      throw new Error(`Unable to parse JSONC content: ${errors.length} errors`);
    }
    return result;
  };
  return [
    parseJSONC,
    (text) => toml.parse(text),
    (text) => yaml.load(text, { maxAliases: MAX_MANIFEST_ALIASES, maxDepth: MAX_MANIFEST_DEPTH }),
  ];
}

function resolveRoot(root) {
  try {
    return fs.realpathSync(path.resolve(root));
  } catch {
    fail(`refusing root folder that does not resolve: ${root}`);
  }
}

// Refuses an input that is absolute, climbs with "..", or resolves outside the folder it is run from (the staged
// skill), before any install, read or write.
function checkedInput(file, realRoot) {
  if (typeof file !== "string" || file === "" || path.isAbsolute(file) || file.split(/[\\/]+/).includes("..")) {
    fail(`refusing Markdown input outside the skill folder: ${file}`);
  }
  const candidate = path.resolve(realRoot, file);
  let resolved;
  try {
    resolved = fs.realpathSync(candidate);
  } catch {
    fail(`refusing Markdown input that does not resolve: ${file}`);
  }
  const relative = path.relative(realRoot, resolved);
  if (relative === "" || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    fail(`refusing Markdown input outside the skill folder: ${file}`);
  }
  return { file, resolved };
}

function readLintSource(resolvedPath) {
  let descriptor;
  try {
    descriptor = fs.openSync(resolvedPath, "r");
  } catch {
    fail(`refusing to normalize non-file Markdown source ${resolvedPath}`);
  }
  try {
    const lstat = fs.lstatSync(resolvedPath);
    if (lstat.isSymbolicLink()) {
      fail(`refusing to normalize non-file Markdown source ${resolvedPath}`);
    }
    const stat = fs.fstatSync(descriptor);
    if (!stat.isFile()) {
      fail(`refusing to normalize non-file Markdown source ${resolvedPath}`);
    }
    if (stat.size > MAX_FILE_BYTES_CEILING) {
      fail(`${resolvedPath} is ${stat.size} bytes; per-file ceiling is ${MAX_FILE_BYTES_CEILING}`);
    }
    return fs.readFileSync(descriptor, "utf8");
  } finally {
    fs.closeSync(descriptor);
  }
}

function normalizeOne(entry, lint, applyFixes, baseOptions) {
  const original = readLintSource(entry.resolved);
  const initial = lint({ ...baseOptions, config: NORMALIZE_CONFIG, strings: { [entry.file]: original } })[entry.file] ?? [];
  if (initial.length === 0) {
    return [];
  }
  const rules = [...new Set(initial.map((item) => item.ruleNames[0]))].sort();
  let current = applyFixes(original, initial);
  for (let pass = 0; pass < MAX_FIX_PASSES; pass += 1) {
    const remaining = lint({ ...baseOptions, config: NORMALIZE_CONFIG, strings: { [entry.file]: current } })[entry.file] ?? [];
    if (remaining.length === 0) {
      break;
    }
    if (remaining.some((item) => item.ruleNames[0] === "MD060" && !item.fixInfo)) {
      const compact = lint({ ...baseOptions, config: COMPACT_TABLE_CONFIG, strings: { [entry.file]: current } })[entry.file] ?? [];
      current = applyFixes(current, compact);
    } else {
      current = applyFixes(current, remaining);
    }
  }
  if (current === original) {
    return [];
  }
  fs.writeFileSync(entry.resolved, current, "utf8");
  return rules;
}

function parseArgs(args) {
  let root = process.cwd();
  const files = [];
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--root" && index + 1 < args.length) {
      index += 1;
      root = args[index];
    } else if (arg.startsWith("--root=")) {
      root = arg.slice("--root=".length);
    } else {
      files.push(arg);
    }
  }
  return { root, files };
}

async function main() {
  const { root, files } = parseArgs(process.argv.slice(2));
  if (files.length === 0) {
    process.stdout.write("{}\n");
    return;
  }
  if (files.length > MAX_FILES) {
    fail(`normalize batch has ${files.length} files; maximum is ${MAX_FILES}`);
  }
  const realRoot = resolveRoot(root);
  const entries = [];
  for (let index = 0; index < files.length; index += 1) {
    entries.push(checkedInput(files[index], realRoot));
  }
  const toolDir = path.dirname(fileURLToPath(import.meta.url));
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "praetor-markdownlint-normalize-"));
  try {
    install(toolDir, temporary);
    const { rootEntry, syncEntry } = markdownlintEntries(temporary);
    const { lint } = await import(pathToFileURL(syncEntry).href);
    const { applyFixes } = await import(pathToFileURL(rootEntry).href);
    const baseOptions = {
      configParsers: configurationParsers(temporary),
      handleRuleFailures: true,
      noInlineConfig: false,
    };
    const changed = {};
    for (let index = 0; index < entries.length && index < MAX_FILES; index += 1) {
      const entry = entries[index];
      const rules = normalizeOne(entry, lint, applyFixes, baseOptions);
      if (rules.length > 0) {
        changed[entry.file] = rules;
      }
    }
    process.stdout.write(`${JSON.stringify(changed)}\n`);
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

main().catch((error) => {
  process.stderr.write(`markdown-normalize: ${error.message}\n`);
  process.exitCode = 2;
});
