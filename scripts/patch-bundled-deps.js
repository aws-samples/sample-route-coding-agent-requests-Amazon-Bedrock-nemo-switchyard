#!/usr/bin/env node
/**
 * postinstall: remove the vulnerable `brace-expansion` that aws-cdk-lib bundles.
 *
 * aws-cdk-lib lists `minimatch` in `bundledDependencies`, so its tarball ships
 * a pre-resolved node_modules/aws-cdk-lib/node_modules/brace-expansion that
 * npm `overrides` and `npm audit fix` cannot reach (GHSA-q2hr-2g5m-vwhr and
 * friends, fixed in brace-expansion 5.0.12).
 *
 * Fix: drop the bundled entry from package-lock.json and delete the nested
 * directory. Node resolves modules by walking up the tree, so the bundled
 * minimatch then loads the hoisted top-level brace-expansion, which
 * package.json pins to a patched 5.x release. The script refuses to delete
 * anything unless that hoisted copy exists and satisfies minimatch's `^5`.
 *
 * Idempotent; a no-op once aws-cdk-lib bundles brace-expansion >= 5.0.12.
 * Delete this script, its postinstall hook, and the brace-expansion
 * devDependency at that point.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const FIRST_PATCHED = '5.0.12';
const TAG = '[patch-bundled-deps]';
const ROOT = path.resolve(__dirname, '..');
const LOCKFILE = path.join(ROOT, 'package-lock.json');
const LOCK_KEY = 'node_modules/aws-cdk-lib/node_modules/brace-expansion';
const NESTED_DIR = path.join(ROOT, ...LOCK_KEY.split('/'));
const HOISTED_DIR = path.join(ROOT, 'node_modules', 'brace-expansion');

function readVersion(dir) {
  try {
    return JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8')).version;
  } catch {
    return undefined;
  }
}

function parse(v) {
  return String(v)
    .split('-')[0]
    .split('.')
    .map((n) => Number.parseInt(n, 10) || 0);
}

function isBelow(version, floor) {
  const a = parse(version);
  const b = parse(floor);
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] < b[i];
  }
  return false;
}

function hoistedIsSafe() {
  const hoisted = readVersion(HOISTED_DIR);
  if (!hoisted) {
    console.warn(`${TAG} no hoisted brace-expansion found; leaving the bundled copy in place.`);
    return false;
  }
  if (parse(hoisted)[0] !== 5 || isBelow(hoisted, FIRST_PATCHED)) {
    console.warn(
      `${TAG} hoisted brace-expansion is ${hoisted}; need >= ${FIRST_PATCHED} on the 5.x line. ` +
        'Leaving the bundled copy in place.',
    );
    return false;
  }
  return true;
}

function pruneLockfile() {
  if (!fs.existsSync(LOCKFILE)) return false;
  const lock = JSON.parse(fs.readFileSync(LOCKFILE, 'utf8'));
  const entry = lock.packages?.[LOCK_KEY];
  if (!entry || !isBelow(entry.version, FIRST_PATCHED)) return false;
  delete lock.packages[LOCK_KEY];
  fs.writeFileSync(LOCKFILE, `${JSON.stringify(lock, null, 2)}\n`);
  console.log(`${TAG} removed bundled brace-expansion@${entry.version} from package-lock.json`);
  return true;
}

function pruneNodeModules() {
  const bundled = readVersion(NESTED_DIR);
  if (!bundled || !isBelow(bundled, FIRST_PATCHED)) return false;
  fs.rmSync(NESTED_DIR, { recursive: true, force: true });
  console.log(`${TAG} removed bundled brace-expansion@${bundled} from node_modules`);
  return true;
}

function main() {
  if (!hoistedIsSafe()) return;
  const changed = pruneLockfile() | pruneNodeModules();
  if (!changed) {
    console.log(`${TAG} nothing to do (bundled brace-expansion is absent or already patched)`);
  }
}

main();
