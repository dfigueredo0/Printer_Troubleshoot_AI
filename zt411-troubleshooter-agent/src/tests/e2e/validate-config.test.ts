/**
 * E2E tests — validate-config.sh
 *
 * Replaces the existing file-presence-only script with tests that actually
 * verify the framework is wired correctly end-to-end.
 */

import { describe, it, expect } from 'vitest';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { existsSync, readFileSync } from 'fs';
import { join } from 'path';

const execAsync = promisify(execFile);

// ── File structure ────────────────────────────────────────────────────────────

describe('Required file structure', () => {
  const requiredFiles = [
    '.claude/settings.json',
    '.claude/helpers/hook-handler.cjs',
    '.claude/helpers/auto-memory-hooks.mjs',
    '.claude/helpers/swarm-hooks.sh',
    '.claude/helpers/adr-compliance.sh',
    '.claude/helpers/ddd-tracker.sh',
    '.claude/helpers/security-scanner.sh',
    '.claude/helpers/validate-config.sh',
    '.mcp.json',
    'docs/adr/README.md',
    'docs/ddd/README.md',
    'vitest.config.ts',
    'package.json',
    'tsconfig.json',
  ];

  for (const file of requiredFiles) {
    it(`${file} exists`, () => {
      expect(existsSync(file), `Missing: ${file}`).toBe(true);
    });
  }

  const requiredDirs = [
    '.claude/commands',
    '.claude/commands/swarm',
    '.claude/commands/memory',
    '.claude/commands/hooks',
    '.claude/commands/github',
    '.claude/commands/coordination',
    '.claude/commands/optimization',
    '.claude/commands/analysis',
    '.claude/commands/agents',
    '.claude/agents',
    'docs/adr',
    'docs/ddd',
  ];

  for (const dir of requiredDirs) {
    it(`${dir}/ exists`, () => {
      expect(existsSync(dir), `Missing directory: ${dir}`).toBe(true);
    });
  }
});

// ── settings.json integrity ───────────────────────────────────────────────────

describe('settings.json integrity', () => {
  let settings: Record<string, unknown>;

  it('is valid JSON', () => {
    const raw = readFileSync('.claude/settings.json', 'utf8');
    expect(() => { settings = JSON.parse(raw); }).not.toThrow();
  });

  it('has CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1', () => {
    const s = JSON.parse(readFileSync('.claude/settings.json', 'utf8'));
    expect(s.env?.CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS).toBe('1');
  });

  it('hook commands reference files that exist', () => {
    const s = JSON.parse(readFileSync('.claude/settings.json', 'utf8'));
    const hooks = s.hooks ?? {};
    for (const [event, groups] of Object.entries(hooks as Record<string, unknown[]>)) {
      for (const group of groups) {
        for (const hook of (group as { hooks?: { command?: string }[] }).hooks ?? []) {
          const cmd = hook.command ?? '';
          // Extract node file path from: node "$CLAUDE_PROJECT_DIR/.claude/helpers/foo.cjs" arg
          const match = cmd.match(/node\s+"[^"]*CLAUDE_PROJECT_DIR[^"]*\/helpers\/([^"]+)"/);
          if (match?.[1]) {
            const helperFile = join('.claude/helpers', match[1]);
            expect(
              existsSync(helperFile),
              `Hook "${event}" references missing helper: ${helperFile}`
            ).toBe(true);
          }
        }
      }
    }
  });

  it('does not reference auto-memory-hook.mjs (old filename)', () => {
    const raw = readFileSync('.claude/settings.json', 'utf8');
    expect(raw).not.toContain('auto-memory-hook.mjs"');
  });

  it('does not have a statusLine block', () => {
    const s = JSON.parse(readFileSync('.claude/settings.json', 'utf8'));
    expect(s).not.toHaveProperty('statusLine');
  });

  it('claudeFlow.adr.directory is a relative path', () => {
    const s = JSON.parse(readFileSync('.claude/settings.json', 'utf8'));
    const dir = s.claudeFlow?.adr?.directory ?? '';
    expect(dir).not.toMatch(/^\//);
    expect(dir.length).toBeGreaterThan(0);
  });

  it('permissions.allow contains all four MCP namespaces', () => {
    const s = JSON.parse(readFileSync('.claude/settings.json', 'utf8'));
    const allow: string[] = s.permissions?.allow ?? [];
    expect(allow).toContain('mcp__claude-flow__*');
    expect(allow).toContain('mcp__ruv-swarm__*');
    expect(allow).toContain('mcp__context7__*');
    expect(allow).toContain('mcp__playwright__*');
  });
});

// ── .mcp.json integrity ───────────────────────────────────────────────────────

describe('.mcp.json integrity', () => {
  it('is valid JSON', () => {
    const raw = readFileSync('.mcp.json', 'utf8');
    expect(() => JSON.parse(raw)).not.toThrow();
  });

  it('defines claude-flow, ruv-swarm, context7, playwright servers', () => {
    const mcp = JSON.parse(readFileSync('.mcp.json', 'utf8'));
    const servers = Object.keys(mcp.mcpServers ?? {});
    expect(servers).toContain('claude-flow');
    expect(servers).toContain('ruv-swarm');
    expect(servers).toContain('context7');
    expect(servers).toContain('playwright');
  });
});

// ── ADR documents ─────────────────────────────────────────────────────────────

describe('ADR documents', () => {
  const ADR_IDS = ['001','002','003','004','005','006','007','008','009','010'];

  for (const id of ADR_IDS) {
    it(`ADR-${id}.md exists and has required sections`, () => {
      const path = `docs/adr/ADR-${id}.md`;
      expect(existsSync(path), `Missing ${path}`).toBe(true);

      const content = readFileSync(path, 'utf8');
      expect(content).toContain('## Status');
      expect(content).toContain('## Context');
      expect(content).toContain('## Decision');
      expect(content).toContain('## Consequences');
    });
  }
});

// ── DDD documents ─────────────────────────────────────────────────────────────

describe('DDD domain documents', () => {
  const DOMAINS = [
    'agent-lifecycle',
    'task-execution',
    'memory-management',
    'coordination',
    'shared-kernel',
  ];

  for (const domain of DOMAINS) {
    it(`${domain}.md exists and documents entities and events`, () => {
      const path = `docs/ddd/${domain}.md`;
      expect(existsSync(path), `Missing ${path}`).toBe(true);

      const content = readFileSync(path, 'utf8');
      expect(content).toContain('## Responsibility');
      expect(content).toContain('Domain events');
    });
  }
});

// ── Command layer ─────────────────────────────────────────────────────────────

describe('commands layer population', () => {
  const commandDirs: Record<string, number> = {
    '.claude/commands/swarm':        1,
    '.claude/commands/memory':       1,
    '.claude/commands/hooks':        1,
    '.claude/commands/github':       1,
    '.claude/commands/coordination': 1,
    '.claude/commands/optimization': 1,
    '.claude/commands/analysis':     1,
    '.claude/commands/agents':       1,
  };

  for (const [dir, minFiles] of Object.entries(commandDirs)) {
    it(`${dir}/ has at least ${minFiles} .md file(s)`, () => {
      const files = require('fs').readdirSync(dir).filter((f: string) => f.endsWith('.md'));
      expect(files.length, `${dir} is empty`).toBeGreaterThanOrEqual(minFiles);
    });
  }
});
