/**
 * tests/e2e/validate-config.test.ts
 *
 * Runs validate-config.sh as a subprocess and asserts it exits 0.
 * Also verifies that it produces machine-parseable output for CI.
 */

import { describe, it, expect } from 'vitest';
import { spawnSync } from 'child_process';
import { join }      from 'path';

const ROOT   = process.cwd();
const SCRIPT = join(ROOT, '.claude/helpers/validate-config.sh');

describe('validate-config.sh', () => {
  it('exits 0 when the project is correctly set up', () => {
    const result = spawnSync('bash', [SCRIPT], {
      cwd:      ROOT,
      encoding: 'utf8',
      timeout:  15000,
    });

    if (result.status !== 0) {
      console.error('validate-config.sh stdout:\n', result.stdout);
      console.error('validate-config.sh stderr:\n', result.stderr);
    }

    expect(result.status).toBe(0);
  });

  it('produces a summary line with ERRORS and WARNINGS counts', () => {
    const result = spawnSync('bash', [SCRIPT], {
      cwd:      ROOT,
      encoding: 'utf8',
      timeout:  15000,
    });

    // Should contain either "All checks passed" or error/warning counts
    const output = result.stdout + result.stderr;
    const hasSummary =
      output.includes('All checks passed') ||
      output.includes('errors found') ||
      output.includes('warnings found') ||
      output.includes('Validation Summary');

    expect(hasSummary).toBe(true);
  });
});
