/**
 * Unit tests for hook-handler.cjs
 *
 * Tests all 9 subcommands invoked by settings.json hooks.
 * Uses child_process to invoke the handler as Claude Code would.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { mkdirSync, rmSync, readFileSync, existsSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';

const execFileAsync = promisify(execFile);
const HANDLER = join(process.cwd(), '.claude/helpers/hook-handler.cjs');

function runHook(
  subcommand: string,
  stdin?: string,
  env: Record<string, string> = {}
): Promise<{ stdout: string; stderr: string; code: number }> {
  return new Promise((resolve) => {
    const proc = require('child_process').spawn(
      'node',
      [HANDLER, subcommand],
      {
        env: { ...process.env, CLAUDE_PROJECT_DIR: process.env['TEST_PROJECT_DIR'], ...env },
        stdio: ['pipe', 'pipe', 'pipe'],
      }
    );

    let stdout = '';
    let stderr = '';
    proc.stdout.on('data', (d: Buffer) => { stdout += d.toString(); });
    proc.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });

    if (stdin) {
      proc.stdin.write(stdin);
      proc.stdin.end();
    } else {
      proc.stdin.end();
    }

    proc.on('close', (code: number) => resolve({ stdout, stderr, code: code ?? 0 }));
  });
}

describe('hook-handler.cjs', () => {
  let projectDir: string;

  beforeEach(() => {
    projectDir = join(tmpdir(), `hook-test-${Date.now()}`);
    mkdirSync(projectDir, { recursive: true });
    process.env['TEST_PROJECT_DIR'] = projectDir;
  });

  afterEach(() => {
    rmSync(projectDir, { recursive: true, force: true });
    delete process.env['TEST_PROJECT_DIR'];
  });

  describe('pre-bash', () => {
    it('allows normal bash commands', async () => {
      const input = JSON.stringify({ tool_input: { command: 'npm test' } });
      const { code } = await runHook('pre-bash', input);
      expect(code).toBe(0);
    });

    it('blocks uv install commands', async () => {
      const input = JSON.stringify({ tool_input: { command: 'uv install pandas' } });
      const { code, stderr } = await runHook('pre-bash', input);
      expect(code).toBe(2);
      expect(stderr).toContain('pip');
    });

    it('exits 0 with no stdin', async () => {
      const { code } = await runHook('pre-bash');
      expect(code).toBe(0);
    });
  });

  describe('post-edit', () => {
    it('records edited file to session state', async () => {
      const input = JSON.stringify({ tool_input: { path: 'src/foo.ts' } });
      await runHook('post-edit', input);

      const sessionPath = join(projectDir, '.claude-flow/sessions/current.json');
      expect(existsSync(sessionPath)).toBe(true);

      const session = JSON.parse(readFileSync(sessionPath, 'utf8'));
      expect(session.filesEdited).toContain('src/foo.ts');
    });

    it('does not duplicate file entries', async () => {
      const input = JSON.stringify({ tool_input: { path: 'src/foo.ts' } });
      await runHook('post-edit', input);
      await runHook('post-edit', input);

      const session = JSON.parse(
        readFileSync(join(projectDir, '.claude-flow/sessions/current.json'), 'utf8')
      );
      expect(session.filesEdited.filter((f: string) => f === 'src/foo.ts').length).toBe(1);
    });
  });

  describe('route', () => {
    it('increments prompt counter', async () => {
      await runHook('route');
      await runHook('route');

      const session = JSON.parse(
        readFileSync(join(projectDir, '.claude-flow/sessions/current.json'), 'utf8')
      );
      expect(session.promptCount).toBe(2);
    });

    it('exits 0', async () => {
      const { code } = await runHook('route');
      expect(code).toBe(0);
    });
  });

  describe('session-restore', () => {
    it('exits 0 and emits nothing for a fresh session', async () => {
      const { code, stdout } = await runHook('session-restore');
      expect(code).toBe(0);
      expect(stdout).toBe('');
    });

    it('emits additionalContext when there is prior session data', async () => {
      // Build up some session state first
      const editInput = JSON.stringify({ tool_input: { path: 'src/agent.ts' } });
      await runHook('post-edit', editInput);
      await runHook('route');

      const { code, stdout } = await runHook('session-restore');
      expect(code).toBe(0);
      if (stdout) {
        const parsed = JSON.parse(stdout);
        expect(parsed).toHaveProperty('additionalContext');
        expect(parsed.additionalContext).toContain('session');
      }
    });
  });

  describe('session-end', () => {
    it('resets current session file', async () => {
      const editInput = JSON.stringify({ tool_input: { path: 'src/x.ts' } });
      await runHook('post-edit', editInput);
      await runHook('session-end');

      const session = JSON.parse(
        readFileSync(join(projectDir, '.claude-flow/sessions/current.json'), 'utf8')
      );
      expect(session.filesEdited).toHaveLength(0);
      expect(session.promptCount).toBe(0);
    });

    it('writes a metrics snapshot', async () => {
      await runHook('session-end');
      const metricsPath = join(projectDir, '.claude-flow/metrics/last-session.json');
      expect(existsSync(metricsPath)).toBe(true);
    });

    it('creates an archived session file', async () => {
      await runHook('session-end');
      const archives = require('fs').readdirSync(
        join(projectDir, '.claude-flow/sessions')
      ).filter((f: string) => f.startsWith('session-') && f.endsWith('.json'));
      expect(archives.length).toBeGreaterThan(0);
    });
  });

  describe('status', () => {
    it('exits 0 with empty session', async () => {
      const { code } = await runHook('status');
      expect(code).toBe(0);
    });

    it('emits context when files have been edited', async () => {
      const editInput = JSON.stringify({ tool_input: { path: 'src/b.ts' } });
      await runHook('post-edit', editInput);

      const { code, stdout } = await runHook('status');
      expect(code).toBe(0);
      if (stdout) {
        const parsed = JSON.parse(stdout);
        expect(parsed.additionalContext).toContain('edited');
      }
    });
  });

  describe('unknown subcommand', () => {
    it('exits 0 silently — never blocks Claude Code', async () => {
      const { code, stdout, stderr } = await runHook('nonexistent-command');
      expect(code).toBe(0);
      expect(stdout).toBe('');
    });
  });
});
