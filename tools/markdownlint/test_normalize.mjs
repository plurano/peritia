// SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
//
// SPDX-License-Identifier: EUPL-1.2

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const toolDir = path.dirname(fileURLToPath(import.meta.url));
const normalizeScript = path.join(toolDir, "normalize.mjs");

test("normalize.mjs refuses a path outside the root", () => {
  const tempBase = fs.mkdtempSync(path.join(os.tmpdir(), "normalize-test-outside-"));
  try {
    const rootFolder = path.join(tempBase, "root");
    fs.mkdirSync(rootFolder);
    const outsideFile = path.join(tempBase, "outside.md");
    fs.writeFileSync(outsideFile, "# Outside\n", "utf8");

    // 1. Absolute path outside root
    const absRes = spawnSync(process.execPath, [normalizeScript, outsideFile], {
      cwd: rootFolder,
      encoding: "utf8",
    });
    assert.notEqual(absRes.status, 0);
    assert.match(absRes.stderr, /refusing Markdown input outside the skill folder/u);

    // 2. Relative climbing path outside root
    const climbRes = spawnSync(process.execPath, [normalizeScript, "../outside.md"], {
      cwd: rootFolder,
      encoding: "utf8",
    });
    assert.notEqual(climbRes.status, 0);
    assert.match(climbRes.stderr, /refusing Markdown input outside the skill folder/u);

    // 3. Symlink pointing outside root
    const symlinkFile = path.join(rootFolder, "symlink.md");
    fs.symlinkSync(outsideFile, symlinkFile);
    const symlinkRes = spawnSync(process.execPath, [normalizeScript, "symlink.md"], {
      cwd: rootFolder,
      encoding: "utf8",
    });
    assert.notEqual(symlinkRes.status, 0);
    assert.match(symlinkRes.stderr, /refusing Markdown input outside the skill folder/u);

    // 4. Using explicit --root flag
    const explicitRes = spawnSync(process.execPath, [normalizeScript, "--root", rootFolder, outsideFile], {
      cwd: tempBase,
      encoding: "utf8",
    });
    assert.notEqual(explicitRes.status, 0);
    assert.match(explicitRes.stderr, /refusing Markdown input outside the skill folder/u);

    // Outside file must remain untouched
    assert.equal(fs.readFileSync(outsideFile, "utf8"), "# Outside\n");
  } finally {
    fs.rmSync(tempBase, { recursive: true, force: true });
  }
});

test("normalize.mjs processes a path inside the root", () => {
  const tempBase = fs.mkdtempSync(path.join(os.tmpdir(), "normalize-test-inside-"));
  try {
    const rootFolder = path.join(tempBase, "root");
    fs.mkdirSync(rootFolder);
    const insideFile = path.join(rootFolder, "inside.md");
    // Markdown with heading spacing (MD022), list spacing (MD032), table (MD060), and EOF newline (MD047)
    const unalignedMd =
      "# Reference\n" +
      "## Tight Heading\n" +
      "Intro paragraph:\n" +
      "- item one\n" +
      "```python\n" +
      "x = 1\n" +
      "```\n" +
      "After fence.\n\n" +
      "| Header One | Header Two |\n" +
      "|---|---|\n" +
      "| 1 | 2 |";
    fs.writeFileSync(insideFile, unalignedMd, "utf8");

    const res = spawnSync(process.execPath, [normalizeScript, "inside.md"], {
      cwd: rootFolder,
      encoding: "utf8",
    });
    assert.equal(res.status, 0, res.stderr);

    const changed = JSON.parse(res.stdout);
    assert.ok(Array.isArray(changed["inside.md"]), `expected rules for inside.md, got ${res.stdout}`);
    assert.ok(changed["inside.md"].includes("MD022"), `expected MD022 rule, got ${JSON.stringify(changed)}`);
    assert.ok(changed["inside.md"].includes("MD047"), `expected MD047 rule, got ${JSON.stringify(changed)}`);

    const normalizedContent = fs.readFileSync(insideFile, "utf8");
    assert.notEqual(normalizedContent, unalignedMd);
    assert.ok(normalizedContent.endsWith("\n"));
  } finally {
    fs.rmSync(tempBase, { recursive: true, force: true });
  }
});
