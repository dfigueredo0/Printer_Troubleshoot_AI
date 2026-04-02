/**
 * Performance benchmark tests
 *
 * Validates the targets defined in .claude/config/performance-targets.json
 * Phase 2 targets (the baseline CI gate):
 *   - CLI startup:           < 500ms
 *   - Agent spawn:           < 200ms
 *   - MCP response (p95):    < 100ms
 *   - Memory search (HNSW):  < 10ms per query
 *   - Hook handler:          < 50ms per invocation
 *   - Session restore:       < 100ms
 *
 * Run with: npm run benchmark
 */

import { describe, it, expect } from 'vitest';
import { execFile, spawn } from 'child_process';
import { promisify } from 'util';
import { join } from 'path';
import { mkdirSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';

const execAsync = promisify(execFile);

/** Measure wall-clock time for an async operation */
async function measure<T>(fn: () => Promise<T>): Promise<{ result: T; ms: number }> {
  const start = performance.now();
  const result = await fn();
  return { result, ms: performance.now() - start };
}

/** Run hook-handler and return elapsed ms */
function measureHook(subcommand: string, projectDir: string): Promise<number> {
  return new Promise((resolve, reject) => {
    const start = performance.now();
    const proc = spawn('node', [
      join(process.cwd(), '.claude/helpers/hook-handler.cjs'),
      subcommand,
    ], {
      env: { ...process.env, CLAUDE_PROJECT_DIR: projectDir },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    proc.stdin.end();
    proc.on('close', () => resolve(performance.now() - start));
    proc.on('error', reject);
  });
}

describe('Hook handler performance', () => {
  let projectDir: string;

  // Warm-up then measure
  it('route hook < 50ms (p95 over 20 calls)', async () => {
    projectDir = join(tmpdir(), `perf-${Date.now()}`);
    mkdirSync(projectDir, { recursive: true });

    const samples: number[] = [];
    for (let i = 0; i < 20; i++) {
      samples.push(await measureHook('route', projectDir));
    }

    samples.sort((a, b) => a - b);
    const p95 = samples[Math.floor(samples.length * 0.95)] ?? samples[samples.length - 1]!;

    console.log(`  route p50: ${samples[Math.floor(samples.length * 0.5)]?.toFixed(1)}ms`);
    console.log(`  route p95: ${p95.toFixed(1)}ms`);

    expect(p95).toBeLessThan(50);

    rmSync(projectDir, { recursive: true, force: true });
  }, 30_000);

  it('session-restore hook < 100ms cold start', async () => {
    projectDir = join(tmpdir(), `perf-sr-${Date.now()}`);
    mkdirSync(projectDir, { recursive: true });

    const ms = await measureHook('session-restore', projectDir);
    console.log(`  session-restore: ${ms.toFixed(1)}ms`);
    expect(ms).toBeLessThan(100);

    rmSync(projectDir, { recursive: true, force: true });
  });

  it('post-edit hook < 50ms', async () => {
    projectDir = join(tmpdir(), `perf-pe-${Date.now()}`);
    mkdirSync(projectDir, { recursive: true });

    const ms = await measureHook('post-edit', projectDir);
    console.log(`  post-edit: ${ms.toFixed(1)}ms`);
    expect(ms).toBeLessThan(50);

    rmSync(projectDir, { recursive: true, force: true });
  });
});

describe('Memory hook performance', () => {
  const HOOK = join(process.cwd(), '.claude/helpers/auto-memory-hooks.mjs');

  function measureMemHook(sub: string, args: string[], projectDir: string): Promise<number> {
    return new Promise((resolve, reject) => {
      const start = performance.now();
      const proc = spawn('node', [HOOK, sub, ...args], {
        env: { ...process.env, CLAUDE_PROJECT_DIR: projectDir },
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      proc.stdin.end();
      proc.on('close', () => resolve(performance.now() - start));
      proc.on('error', reject);
    });
  }

  it('memory store < 20ms', async () => {
    const dir = join(tmpdir(), `perf-ms-${Date.now()}`);
    mkdirSync(dir, { recursive: true });

    const ms = await measureMemHook('store', ['key', 'value'], dir);
    console.log(`  memory store: ${ms.toFixed(1)}ms`);
    expect(ms).toBeLessThan(20);

    rmSync(dir, { recursive: true, force: true });
  });

  it('memory search < 10ms over 100 entries (p95 over 20 queries)', async () => {
    const dir = join(tmpdir(), `perf-search-${Date.now()}`);
    mkdirSync(join(dir, '.claude-flow/memory'), { recursive: true });

    // Populate 100 entries
    const entries = Array.from({ length: 100 }, (_, i) => ({
      key: `pattern-${i}`,
      value: `This is pattern number ${i} about ${['auth', 'memory', 'coordination', 'testing', 'performance'][i % 5]}`,
      timestamp: new Date(Date.now() - i * 1000).toISOString(),
      type: 'manual',
    }));
    writeFileSync(
      join(dir, '.claude-flow/memory/index.json'),
      JSON.stringify({ entries })
    );

    const samples: number[] = [];
    for (let i = 0; i < 20; i++) {
      samples.push(await measureMemHook('search', ['auth'], dir));
    }

    samples.sort((a, b) => a - b);
    const p95 = samples[Math.floor(samples.length * 0.95)] ?? samples[samples.length - 1]!;
    console.log(`  memory search p95: ${p95.toFixed(1)}ms`);
    expect(p95).toBeLessThan(10);

    rmSync(dir, { recursive: true, force: true });
  }, 30_000);
});

describe('Settings.json validation performance', () => {
  it('settings.json parses in < 5ms', async () => {
    const { ms } = await measure(async () => {
      return JSON.parse(
        require('fs').readFileSync('.claude/settings.json', 'utf8')
      );
    });
    console.log(`  settings.json parse: ${ms.toFixed(2)}ms`);
    expect(ms).toBeLessThan(5);
  });
});
