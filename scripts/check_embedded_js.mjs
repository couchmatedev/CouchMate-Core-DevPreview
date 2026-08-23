#!/usr/bin/env node

// Syntax-check the browser code embedded in the two Home Assistant pages.
// No browser globals are executed: Function construction only parses the JS.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pages = [
  "custom_components/couchmate_dev/configurator.py",
  "custom_components/couchmate_dev/management.py",
];

for (const relativePath of pages) {
  const source = fs.readFileSync(path.join(root, relativePath), "utf8");
  const scripts = [...source.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (scripts.length !== 1) {
    throw new Error(`${relativePath}: expected one embedded script, found ${scripts.length}`);
  }

  try {
    // eslint-disable-next-line no-new-func
    new Function(scripts[0][1]);
  } catch (error) {
    throw new Error(`${relativePath}: embedded JavaScript is invalid: ${error.message}`);
  }

  const html = source.slice(source.indexOf("<!doctype html>"));
  const staticIds = [...html.matchAll(/\sid=["']([^"']+)["']/g)].map((match) => match[1]);
  const duplicates = [...new Set(staticIds.filter((id, index) => staticIds.indexOf(id) !== index))];
  if (duplicates.length) {
    throw new Error(`${relativePath}: duplicate static HTML ids: ${duplicates.join(", ")}`);
  }

  process.stdout.write(`${relativePath}: embedded JavaScript syntax PASS\n`);
}
