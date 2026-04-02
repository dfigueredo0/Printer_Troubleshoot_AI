/**
 * tests/unit/auto-memory-hooks.test.ts
 *
 * Tests for .claude/helpers/auto-memory-hooks.mjs
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { spawnSync } from 'child_process';
import { mkdirSync, rmSync, writeFileSync, readFileSync, existsSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';

const HOOK = join(process.cwd(), '.claude/helpers/auto-memory-hooks.mjs');

function run(subcommand: string, args: string[] = [], env: Record<string, string> = {}) {
  return spawnSync(
    'node',
    [HOOK, subcommand, ...args],
    {
      encoding: 'utf8',
      env: { ...process.env, ...env },
      timeout: 4000,
    }
  );
}

function tempProjectDir() {
  const dir = join(tmpdir(), `memory-hooks-test-${Date.now()}`);
  mkdirSync(dir, { recursive: true });
  return dir;
}

describe('auto-memory-hooks.mjs — protocol compliance', () => {
  it('exits 0 for an unknown subcommand', () => {
    const result = run('unknown');
    expect(result.status).toBe(0);
  });
});

describe('auto-memory-hooks.mjs — import', () => {
  let projectDir: string;

  beforeEach(() => { projectDir = tempProjectDir(); });
  afterEach(() => { rmSync(projectDir, { recursive: true, force: true }); });

  it('exits 0 silently when the memory index does not exist yet', () => {
    const result = run('import', [], { CLAUDE_PROJECT_DIR: projectDir });
    expect(result.status).toBe(0);
    expect(result.stdout).toBe('');
  });

  it('exits 0 silently when the memory index is empty', () => {
    const memDir = join(projectDir, '.claude-flow/memory');
    mkdirSync(memDir, { recursive: true });
    writeFileSync(join(memDir, 'index.json'), JSON.stringify({ entries: [] }));

    const result = run('import', [], { CLAUDE_PROJECT_DIR: projectDir });
    expect(result.status).toBe(0);
    expect(result.stdout).toBe('');
  });

  it('emits additionalContext JSON when memory entries exist', () => {
    const memDir = join(projectDir, '.claude-flow/memory');
    mkdirSync(memDir, { recursive: true });
    writeFileSync(join(memDir, 'index.json'), JSON.stringify({
      entries: [
        { key: 'file:/src/auth.ts', value: 'edited in session_1', timestamp: '2025-01-02T00:00:00.000Z', type: 'file-edit' },
        { key: 'task:session_1',   value: 'Implemented JWT tokens', timestamp: '2025-01-01T00:00:00.000Z', type: 'task' },
      ],
    }));

    const result = run('import', [], { CLAUDE_PROJECT_DIR: projectDir });
    expect(result.status).toBe(0);
    expect(result.stdout).not.toBe('');

    const parsed = JSON.parse(result.stdout);
    expect(parsed).toHaveProperty('additionalContext');
    expect(parsed.additionalContext).toContain('auth.ts');
    expect(parsed.additionalContext).toContain('JWT');
  });

  it('surfaces at most 10 entries even with a large index', () => {
    const memDir = join(projectDir, '.claude-flow/memory');
    mkdirSync(memDir, { recursive: true });
    const entries = Array.from({ length: 25 }, (_, i) => ({
      key: `file:entry-${i}`,
      value: `value ${i}`,
      timestamp: new Date(Date.now() - i * 1000).toISOString(),
      type: 'file-edit',
    }));
    writeFileSync(join(memDir, 'index.json'), JSON.stringify({ entries }));

    const result = run('import', [], { CLAUDE_PROJECT_DIR: projectDir });
    const parsed = JSON.parse(result.stdout);
    // Count bullet points — max 10 entries
    const bulletCount = (parsed.additionalContext.match(/^- /gm) || []).length;
    expect(bulletCount).toBeLessThanOrEqual(10);
  });
});

describe('auto-memory-hooks.mjs — sync', () => {
  let projectDir: string;

  beforeEach(() => { projectDir = tempProjectDir(); });
  afterEach(() => { rmSync(projectDir, { recursive: true, force: true }); });

  it('exits 0 even when no session state exists', () => {
    const result = run('sync', [], { CLAUDE_PROJECT_DIR: projectDir });
    expect(result.status).toBe(0);
  });

  it('persists edited files from session state into the memory index', () => {
    const sessionDir = join(projectDir, '.claude-flow/sessions');
    mkdirSync(sessionDir, { recursive: true });
    writeFileSync(join(sessionDir, 'current.json'), JSON.stringify({
      id: 'session_test',
      startedAt: new Date().toISOString(),
      filesEdited: ['/src/coordinator.ts', '/src/memory.ts'],
      tasksCompleted: ['Refactored coordinator'],
      promptCount: 4,
    }));

    run('sync', [], { CLAUDE_PROJECT_DIR: projectDir });

    const indexPath = join(projectDir, '.claude-flow/memory/index.json');
    expect(existsSync(indexPath)).toBe(true);

    const index = JSON.parse(readFileSync(indexPath, 'utf8'));
    const keys = index.entries.map((e: { key: string }) => e.key);
    expect(keys).toContain('file:/src/coordinator.ts');
    expect(keys).toContain('file:/src/memory.ts');
  });

  it('trims the index to 200 entries maximum', () => {
    // Seed a large existing index
    const memDir = join(projectDir, '.claude-flow/memory');
    mkdirSync(memDir, { recursive: true });
    const existing = Array.from({ length: 210 }, (_, i) => ({
      key: `old-entry-${i}`,
      value: `v${i}`,
      timestamp: new Date(Date.now() - i * 10000).toISOString(),
      type: 'file-edit',
    }));
    writeFileSync(join(memDir, 'index.json'), JSON.stringify({ entries: existing }));

    run('sync', [], { CLAUDE_PROJECT_DIR: projectDir });

    const index = JSON.parse(readFileSync(join(memDir, 'index.json'), 'utf8'));
    expect(index.entries.length).toBeLessThanOrEqual(200);
  });

  it('updates lastSync timestamp', () => {
    run('sync', [], { CLAUDE_PROJECT_DIR: projectDir });

    const indexPath = join(projectDir, '.claude-flow/memory/index.json');
    const index = JSON.parse(readFileSync(indexPath, 'utf8'));
    expect(index).toHaveProperty('lastSync');
    expect(new Date(index.lastSync).getTime()).toBeGreaterThan(Date.now() - 5000);
  });
});

describe('auto-memory-hooks.mjs — store and search', () => {
  let projectDir: string;

  beforeEach(() => { projectDir = tempProjectDir(); });
  afterEach(() => { rmSync(projectDir, { recursive: true, force: true }); });

  it('stores a key-value pair and confirms in stdout', () => {
    const result = run('store', ['my-key', 'my-value'], { CLAUDE_PROJECT_DIR: projectDir });
    expect(result.status).toBe(0);
    const parsed = JSON.parse(result.stdout);
    expect(parsed.stored).toBe(true);
    expect(parsed.key).toBe('my-key');
  });

  it('retrieves a stored value via search', () => {
    run('store', ['search-key', 'findable content'], { CLAUDE_PROJECT_DIR: projectDir });

    const result = run('search', ['findable'], { CLAUDE_PROJECT_DIR: projectDir });
    expect(result.status).toBe(0);

    const parsed = JSON.parse(result.stdout);
    expect(parsed.count).toBeGreaterThan(0);
    expect(parsed.results.some((r: { key: string }) => r.key === 'search-key')).toBe(true);
  });

  it('overwrites an existing key on repeated store', () => {
    run('store', ['dup-key', 'first'],  { CLAUDE_PROJECT_DIR: projectDir });
    run('store', ['dup-key', 'second'], { CLAUDE_PROJECT_DIR: projectDir });

    const result = run('search', ['dup-key'], { CLAUDE_PROJECT_DIR: projectDir });
    const parsed = JSON.parse(result.stdout);
    const entry = parsed.results.find((r: { key: string }) => r.key === 'dup-key');
    expect(entry.value).toBe('second');
    expect(parsed.count).toBe(1);
  });

  it('returns count 0 and empty results for a non-matching query', () => {
    const result = run('search', ['xyzzy-no-match-abc'], { CLAUDE_PROJECT_DIR: projectDir });
    expect(result.status).toBe(0);
    const parsed = JSON.parse(result.stdout);
    expect(parsed.count).toBe(0);
    expect(parsed.results).toHaveLength(0);
  });
});
