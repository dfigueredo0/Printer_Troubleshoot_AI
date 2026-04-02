/**
 * Unit tests for swarm-hooks.sh
 * Tests agent registration, messaging, and handoff protocols.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdirSync, rmSync, existsSync, readFileSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';
import { promisify } from 'util';
import { execFile } from 'child_process';

const execAsync = promisify(execFile);
const SCRIPT = join(process.cwd(), '.claude/helpers/swarm-hooks.sh');

function runSwarm(
  subcommand: string,
  args: string[] = [],
  env: Record<string, string> = {}
): Promise<{ stdout: string; stderr: string; code: number }> {
  return new Promise((resolve) => {
    const proc = require('child_process').spawn(
      'bash',
      [SCRIPT, subcommand, ...args],
      {
        env: {
          ...process.env,
          AGENTIC_FLOW_AGENT_ID: 'test-agent-001',
          AGENTIC_FLOW_AGENT_NAME: 'test-agent',
          ...env,
        },
        stdio: ['pipe', 'pipe', 'pipe'],
        cwd: process.env['TEST_PROJECT_DIR'],
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

describe('swarm-hooks.sh', () => {
  let projectDir: string;

  beforeEach(() => {
    projectDir = join(tmpdir(), `swarm-test-${Date.now()}`);
    mkdirSync(projectDir, { recursive: true });
    process.env['TEST_PROJECT_DIR'] = projectDir;
  });

  afterEach(() => {
    rmSync(projectDir, { recursive: true, force: true });
    delete process.env['TEST_PROJECT_DIR'];
  });

  describe('agents', () => {
    it('returns JSON with agents array', async () => {
      const { code, stdout } = await runSwarm('agents');
      expect(code).toBe(0);
      const result = JSON.parse(stdout);
      expect(result).toHaveProperty('agents');
      expect(Array.isArray(result.agents)).toBe(true);
    });

    it('registers the current agent on first call', async () => {
      await runSwarm('agents');
      const agentsFile = join(projectDir, '.claude-flow/swarm/agents.json');
      expect(existsSync(agentsFile)).toBe(true);
      const data = JSON.parse(readFileSync(agentsFile, 'utf8'));
      const found = data.agents.find((a: { id: string }) => a.id === 'test-agent-001');
      expect(found).toBeDefined();
    });
  });

  describe('stats', () => {
    it('returns JSON with agent stats', async () => {
      const { code, stdout } = await runSwarm('stats');
      expect(code).toBe(0);
      const result = JSON.parse(stdout);
      expect(result).toHaveProperty('messagesSent');
      expect(result).toHaveProperty('agentId');
    });
  });

  describe('messaging', () => {
    it('send + messages roundtrip works', async () => {
      // Agent A sends a message
      await runSwarm('send', ['test-agent-002', 'hello from agent 001', 'context', 'normal']);

      // Agent B receives it
      const { stdout } = await runSwarm('messages', ['10'], {
        AGENTIC_FLOW_AGENT_ID: 'test-agent-002',
        AGENTIC_FLOW_AGENT_NAME: 'test-agent-2',
      });
      const result = JSON.parse(stdout);
      expect(result.count).toBeGreaterThan(0);
      expect(result.messages[0].content).toContain('hello from agent 001');
    });

    it('broadcast reaches all agents', async () => {
      await runSwarm('broadcast', ['broadcast message to everyone']);

      const { stdout } = await runSwarm('messages', ['10'], {
        AGENTIC_FLOW_AGENT_ID: 'any-agent',
        AGENTIC_FLOW_AGENT_NAME: 'any',
      });
      const result = JSON.parse(stdout);
      expect(result.count).toBeGreaterThan(0);
    });
  });

  describe('task handoff', () => {
    it('initiates a handoff and creates a handoff file', async () => {
      const { code, stdout } = await runSwarm('handoff', [
        'test-agent-002',
        'Review the authentication module',
        '{}',
      ]);
      expect(code).toBe(0);
      const result = JSON.parse(stdout);
      expect(result).toHaveProperty('handoffId');
      expect(result.toAgent).toBe('test-agent-002');
      expect(result.status).toBe('pending');
    });

    it('accept-handoff returns context for the receiving agent', async () => {
      const { stdout: initOut } = await runSwarm('handoff', [
        'test-agent-002',
        'Write unit tests for memory service',
        '{}',
      ]);
      const { handoffId } = JSON.parse(initOut);

      const { code, stdout } = await runSwarm('accept-handoff', [handoffId], {
        AGENTIC_FLOW_AGENT_ID: 'test-agent-002',
      });
      expect(code).toBe(0);
      expect(stdout).toContain('Task Handoff Accepted');
      expect(stdout).toContain('Write unit tests for memory service');
    });

    it('pending-handoffs lists handoffs for the correct agent', async () => {
      await runSwarm('handoff', ['target-agent', 'task description', '{}']);

      const { stdout } = await runSwarm('pending-handoffs', [], {
        AGENTIC_FLOW_AGENT_ID: 'target-agent',
        AGENTIC_FLOW_AGENT_NAME: 'target',
      });
      const pending = JSON.parse(stdout);
      expect(Array.isArray(pending)).toBe(true);
      expect(pending.length).toBeGreaterThan(0);
    });
  });

  describe('consensus', () => {
    it('initiates consensus and returns a consensusId', async () => {
      const { code, stdout } = await runSwarm('consensus', [
        'Which topology should we use?',
        'hierarchical,mesh,ring',
        '30000',
      ]);
      expect(code).toBe(0);
      const result = JSON.parse(stdout);
      expect(result).toHaveProperty('consensusId');
      expect(result.options).toContain('hierarchical');
    });
  });
});
