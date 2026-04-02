/**
 * tests/unit/hook-handler.test.ts
 *
 * Tests for .claude/helpers/hook-handler.cjs
 * Spawns the script as a child process to match how Claude Code calls it,
 * capturing stdout/stderr and exit codes exactly as the hook protocol requires.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { spawnSync }   from 'child_process';
import { mkdirSync, rmSync, writeFileSync, readFileSync, existsSync } from 'fs';
import { join }        from 'path';
import { tmpdir }      from 'os';

// ── Helpers ──────────────────────────────────────────────────────────────────

const HANDLER = join(process.cwd(), '.claude/helpers/hook-handler.cjs');

function run(subcommand: string, stdinData = '', env: Record<string, string> = {}) {
  return spawnSync(
    'node',
    [HANDLER, subcommand],
    {
      input:    stdinData,
      encoding: 'utf8',
      env:      { ...process.env, ...env },
      timeout:  4000,
    }
  );
}

function tempProjectDir() {
  const dir = join(tmpdir(), `hook-handler-test-${Date.now()}`);
  mkdirSync(dir, { recursive: true });
  return dir;
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('hook-handler.cjs — hook protocol compliance', () => {
  it('exits 0 for an unknown subcommand (never blocks Claude Code)', () => {
    const result = run('unknown-subcommand-xyz');
    expect(result.status).toBe(0);
  });

  it('exits 0 with no output when called with no subcommand', () => {
    const result = run('');
    expect(result.status).toBe(0);
    expect(result.stdout).toBe('');
  });
});

describe('hook-handler.cjs — pre-bash', () => {
  it('allows safe commands silently (exit 0, no stdout)', () => {
    const input = JSON.stringify({ tool_input: { command: 'npm install' } });
    const result = run('pre-bash', input);
    expect(result.status).toBe(0);
    expect(result.stdout).toBe('');
  });

  it('blocks "uv install" with exit 2 and a JSON reason on stderr', () => {
    const input = JSON.stringify({ tool_input: { command: 'uv install requests' } });
    const result = run('pre-bash', input);
    expect(result.status).toBe(2);
    const reason = JSON.parse(result.stderr);
    expect(reason).toHaveProperty('reason');
    expect(reason.reason).toMatch(/pip/i);
  });

  it('exits 0 when stdin is empty (handles TTY sessions)', () => {
    const result = run('pre-bash', '');
    expect(result.status).toBe(0);
  });
});

describe('hook-handler.cjs — post-edit', () => {
  let projectDir: string;

  beforeEach(() => {
    projectDir = tempProjectDir();
  });

  afterEach(() => {
    rmSync(projectDir, { recursive: true, force: true });
  });

  it('records the edited file path into session state', () => {
    const filePath = '/src/agents/coder.ts';
    const input    = JSON.stringify({ tool_input: { path: filePath } });
    const result   = run('post-edit', input, { CLAUDE_PROJECT_DIR: projectDir });

    expect(result.status).toBe(0);

    const sessionFile = join(projectDir, '.claude-flow/sessions/current.json');
    expect(existsSync(sessionFile)).toBe(true);

    const session = JSON.parse(readFileSync(sessionFile, 'utf8'));
    expect(session.filesEdited).toContain(filePath);
    expect(session.lastEditedFile).toBe(filePath);
  });

  it('does not duplicate the same file path on repeated edits', () => {
    const filePath = '/src/foo.ts';
    const input    = JSON.stringify({ tool_input: { path: filePath } });

    run('post-edit', input, { CLAUDE_PROJECT_DIR: projectDir });
    run('post-edit', input, { CLAUDE_PROJECT_DIR: projectDir });

    const session = JSON.parse(
      readFileSync(join(projectDir, '.claude-flow/sessions/current.json'), 'utf8')
    );
    const occurrences = session.filesEdited.filter((f: string) => f === filePath);
    expect(occurrences).toHaveLength(1);
  });

  it('exits 0 when no path is provided', () => {
    const result = run('post-edit', '{}', { CLAUDE_PROJECT_DIR: projectDir });
    expect(result.status).toBe(0);
  });
});

describe('hook-handler.cjs — route', () => {
  let projectDir: string;

  beforeEach(() => { projectDir = tempProjectDir(); });
  afterEach(() => { rmSync(projectDir, { recursive: true, force: true }); });

  it('increments promptCount on each call', () => {
    run('route', '', { CLAUDE_PROJECT_DIR: projectDir });
    run('route', '', { CLAUDE_PROJECT_DIR: projectDir });
    run('route', '', { CLAUDE_PROJECT_DIR: projectDir });

    const session = JSON.parse(
      readFileSync(join(projectDir, '.claude-flow/sessions/current.json'), 'utf8')
    );
    expect(session.promptCount).toBe(3);
  });
});

describe('hook-handler.cjs — session-restore', () => {
  let projectDir: string;

  beforeEach(() => { projectDir = tempProjectDir(); });
  afterEach(() => { rmSync(projectDir, { recursive: true, force: true }); });

  it('exits 0 silently for a fresh session (nothing to restore)', () => {
    const result = run('session-restore', '', { CLAUDE_PROJECT_DIR: projectDir });
    expect(result.status).toBe(0);
    expect(result.stdout).toBe('');
  });

  it('emits additionalContext JSON when prior session data exists', () => {
    // Seed a session with some data
    const sessionDir = join(projectDir, '.claude-flow/sessions');
    mkdirSync(sessionDir, { recursive: true });
    writeFileSync(join(sessionDir, 'current.json'), JSON.stringify({
      id: 'session_123',
      startedAt: '2025-01-01T00:00:00.000Z',
      filesEdited: ['/src/main.ts'],
      tasksCompleted: ['Implement auth'],
      promptCount: 5,
    }));

    const result = run('session-restore', '', { CLAUDE_PROJECT_DIR: projectDir });
    expect(result.status).toBe(0);

    const parsed = JSON.parse(result.stdout);
    expect(parsed).toHaveProperty('additionalContext');
    expect(parsed.additionalContext).toContain('/src/main.ts');
    expect(parsed.additionalContext).toContain('Implement auth');
  });
});

describe('hook-handler.cjs — session-end / compact', () => {
  let projectDir: string;

  beforeEach(() => { projectDir = tempProjectDir(); });
  afterEach(() => { rmSync(projectDir, { recursive: true, force: true }); });

  it('archives the session and resets current.json', () => {
    const sessionDir = join(projectDir, '.claude-flow/sessions');
    mkdirSync(sessionDir, { recursive: true });
    writeFileSync(join(sessionDir, 'current.json'), JSON.stringify({
      id: 'session_abc',
      startedAt: '2025-01-01T00:00:00.000Z',
      filesEdited: ['/a.ts', '/b.ts'],
      tasksCompleted: [],
      promptCount: 2,
    }));

    run('session-end', '', { CLAUDE_PROJECT_DIR: projectDir });

    // Current session should be reset (empty counts)
    const current = JSON.parse(
      readFileSync(join(sessionDir, 'current.json'), 'utf8')
    );
    expect(current.filesEdited).toHaveLength(0);
    expect(current.promptCount).toBe(0);

    // An archive file should have been created
    const archives = require('fs').readdirSync(sessionDir)
      .filter((f: string) => f.startsWith('session-') && f !== 'current.json');
    expect(archives.length).toBeGreaterThan(0);
  });

  it('writes a metrics snapshot', () => {
    run('session-end', '', { CLAUDE_PROJECT_DIR: projectDir });

    const metricsFile = join(projectDir, '.claude-flow/metrics/last-session.json');
    expect(existsSync(metricsFile)).toBe(true);

    const metrics = JSON.parse(readFileSync(metricsFile, 'utf8'));
    expect(metrics).toHaveProperty('trigger', 'session-end');
    expect(metrics).toHaveProperty('timestamp');
  });

  for (const trigger of ['compact-manual', 'compact-auto'] as const) {
    it(`exits 0 for ${trigger}`, () => {
      const result = run(trigger, '', { CLAUDE_PROJECT_DIR: projectDir });
      expect(result.status).toBe(0);
    });
  }
});

describe('hook-handler.cjs — status', () => {
  let projectDir: string;

  beforeEach(() => { projectDir = tempProjectDir(); });
  afterEach(() => { rmSync(projectDir, { recursive: true, force: true }); });

  it('exits 0 silently when no work has been done', () => {
    const result = run('status', '', { CLAUDE_PROJECT_DIR: projectDir });
    expect(result.status).toBe(0);
    expect(result.stdout).toBe('');
  });

  it('emits a status context when files have been edited', () => {
    const sessionDir = join(projectDir, '.claude-flow/sessions');
    mkdirSync(sessionDir, { recursive: true });
    writeFileSync(join(sessionDir, 'current.json'), JSON.stringify({
      id: 'session_xyz',
      startedAt: new Date().toISOString(),
      filesEdited: ['/src/a.ts', '/src/b.ts'],
      tasksCompleted: ['Task one'],
      promptCount: 3,
    }));

    const result = run('status', '', { CLAUDE_PROJECT_DIR: projectDir });
    expect(result.status).toBe(0);
    const parsed = JSON.parse(result.stdout);
    expect(parsed.additionalContext).toMatch(/2 file/);
  });
});

describe('hook-handler.cjs — post-task', () => {
  let projectDir: string;

  beforeEach(() => { projectDir = tempProjectDir(); });
  afterEach(() => { rmSync(projectDir, { recursive: true, force: true }); });

  it('records a completed task into session memory', () => {
    const taskDesc = 'Implemented OAuth2 login flow';
    const input = JSON.stringify({ tool_response: { result: taskDesc } });

    run('post-task', input, { CLAUDE_PROJECT_DIR: projectDir });

    const session = JSON.parse(
      readFileSync(join(projectDir, '.claude-flow/sessions/current.json'), 'utf8')
    );
    expect(session.tasksCompleted.some((t: string) => t.includes('OAuth2'))).toBe(true);
  });
});
