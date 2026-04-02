/**
 * Unit tests for auto-memory-hooks.mjs
 * Tests the import/sync lifecycle and the in-process store/search API.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdirSync, rmSync, readFileSync, existsSync, writeFileSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';

const HOOK = join(process.cwd(), '.claude/helpers/auto-memory-hooks.mjs');

function runHook(
  subcommand: string,
  args: string[] = [],
  env: Record<string, string> = {}
): Promise<{ stdout: string; stderr: string; code: number }> {
  return new Promise((resolve) => {
    const proc = require('child_process').spawn(
      'node',
      [HOOK, subcommand, ...args],
      {
        env: { ...process.env, CLAUDE_PROJECT_DIR: process.env['TEST_PROJECT_DIR'], ...env },
        stdio: ['pipe', 'pipe', 'pipe'],
      }
    );
    let stdout = '';
    let stderr = '';
    proc.stdout.on('data', (d: Buffer) => { stdout += d.toString(); });
    proc.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });
    proc.stdin.end();
    proc.on('close', (code: number) => resolve({ stdout, stderr, code: code ?? 0 }));
  });
}

describe('auto-memory-hooks.mjs', () => {
  let projectDir: string;

  beforeEach(() => {
    projectDir = join(tmpdir(), `mem-test-${Date.now()}`);
    mkdirSync(projectDir, { recursive: true });
    process.env['TEST_PROJECT_DIR'] = projectDir;
  });

  afterEach(() => {
    rmSync(projectDir, { recursive: true, force: true });
    delete process.env['TEST_PROJECT_DIR'];
  });

  describe('import', () => {
    it('exits 0 with no memory (fresh project)', async () => {
      const { code, stdout } = await runHook('import');
      expect(code).toBe(0);
      expect(stdout).toBe('');
    });

    it('surfaces stored entries as additionalContext', async () => {
      // Pre-populate the memory index
      const memDir = join(projectDir, '.claude-flow/memory');
      mkdirSync(memDir, { recursive: true });
      writeFileSync(join(memDir, 'index.json'), JSON.stringify({
        entries: [
          { key: 'api-pattern', value: 'Always use Result<T> return type', timestamp: new Date().toISOString(), type: 'manual' },
          { key: 'file:src/foo.ts', value: 'edited in session_123', timestamp: new Date().toISOString(), type: 'file-edit' },
        ],
      }));

      const { code, stdout } = await runHook('import');
      expect(code).toBe(0);
      expect(stdout).toBeTruthy();
      const parsed = JSON.parse(stdout);
      expect(parsed).toHaveProperty('additionalContext');
      expect(parsed.additionalContext).toContain('api-pattern');
    });

    it('limits to 10 most recent entries', async () => {
      const memDir = join(projectDir, '.claude-flow/memory');
      mkdirSync(memDir, { recursive: true });
      const entries = Array.from({ length: 20 }, (_, i) => ({
        key: `key-${i}`,
        value: `value-${i}`,
        timestamp: new Date(Date.now() - i * 1000).toISOString(),
        type: 'manual',
      }));
      writeFileSync(join(memDir, 'index.json'), JSON.stringify({ entries }));

      const { stdout } = await runHook('import');
      const parsed = JSON.parse(stdout);
      // Should only surface 10 lines (plus the header)
      const lines = parsed.additionalContext.split('\n').filter((l: string) => l.startsWith('- '));
      expect(lines.length).toBeLessThanOrEqual(10);
    });
  });

  describe('store', () => {
    it('stores a key-value pair and exits 0', async () => {
      const { code, stdout } = await runHook('store', ['my-key', 'my-value']);
      expect(code).toBe(0);
      const result = JSON.parse(stdout);
      expect(result.stored).toBe(true);
      expect(result.key).toBe('my-key');
    });

    it('persists to the memory index file', async () => {
      await runHook('store', ['persisted-key', 'persisted-value']);
      const indexPath = join(projectDir, '.claude-flow/memory/index.json');
      expect(existsSync(indexPath)).toBe(true);
      const index = JSON.parse(readFileSync(indexPath, 'utf8'));
      const found = index.entries.find((e: { key: string }) => e.key === 'persisted-key');
      expect(found).toBeDefined();
      expect(found.value).toBe('persisted-value');
    });

    it('updates existing key rather than duplicating', async () => {
      await runHook('store', ['dup-key', 'first-value']);
      await runHook('store', ['dup-key', 'second-value']);
      const index = JSON.parse(
        readFileSync(join(projectDir, '.claude-flow/memory/index.json'), 'utf8')
      );
      const matches = index.entries.filter((e: { key: string }) => e.key === 'dup-key');
      expect(matches.length).toBe(1);
      expect(matches[0].value).toBe('second-value');
    });
  });

  describe('search', () => {
    beforeEach(async () => {
      await runHook('store', ['auth-pattern', 'use JWT for authentication']);
      await runHook('store', ['db-pattern', 'always use parameterised queries']);
      await runHook('store', ['test-pattern', 'mock external dependencies in unit tests']);
    });

    it('finds entries matching the query', async () => {
      const { code, stdout } = await runHook('search', ['auth']);
      expect(code).toBe(0);
      const result = JSON.parse(stdout);
      expect(result.count).toBeGreaterThan(0);
      expect(result.results[0].key).toContain('auth');
    });

    it('returns empty results for non-matching query', async () => {
      const { stdout } = await runHook('search', ['zzz-nonexistent-xyz']);
      const result = JSON.parse(stdout);
      expect(result.count).toBe(0);
    });

    it('searches both keys and values', async () => {
      const { stdout } = await runHook('search', ['parameterised']);
      const result = JSON.parse(stdout);
      expect(result.count).toBeGreaterThan(0);
    });
  });

  describe('sync', () => {
    it('exits 0 and writes lastSync to the index', async () => {
      await runHook('store', ['sync-test', 'value']);
      const { code } = await runHook('sync');
      expect(code).toBe(0);
      const index = JSON.parse(
        readFileSync(join(projectDir, '.claude-flow/memory/index.json'), 'utf8')
      );
      expect(index.lastSync).toBeTruthy();
    });

    it('trims entries exceeding 200', async () => {
      const memDir = join(projectDir, '.claude-flow/memory');
      mkdirSync(memDir, { recursive: true });
      const entries = Array.from({ length: 250 }, (_, i) => ({
        key: `k-${i}`, value: `v-${i}`,
        timestamp: new Date(Date.now() - i * 1000).toISOString(),
        type: 'manual',
      }));
      writeFileSync(join(memDir, 'index.json'), JSON.stringify({ entries }));

      await runHook('sync');
      const index = JSON.parse(readFileSync(join(memDir, 'index.json'), 'utf8'));
      expect(index.entries.length).toBeLessThanOrEqual(200);
    });
  });
});
