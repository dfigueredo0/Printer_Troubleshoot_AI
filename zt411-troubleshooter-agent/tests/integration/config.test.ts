/**
 * tests/integration/config.test.ts
 *
 * Validates that the project configuration files are internally consistent
 * and that all referenced files actually exist on disk.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync, existsSync } from 'fs';
import { join, resolve } from 'path';

const ROOT = process.cwd();

function readJSON(rel: string) {
  const abs = join(ROOT, rel);
  expect(existsSync(abs), `${rel} must exist`).toBe(true);
  return JSON.parse(readFileSync(abs, 'utf8'));
}

// ── settings.json ─────────────────────────────────────────────────────────────

describe('settings.json', () => {
  const settings = readJSON('.claude/settings.json');

  it('is valid JSON with required top-level keys', () => {
    expect(settings).toHaveProperty('permissions');
    expect(settings).toHaveProperty('hooks');
    expect(settings).toHaveProperty('claudeFlow');
  });

  it('does not reference the old auto-memory-hook.mjs filename', () => {
    const raw = readFileSync(join(ROOT, '.claude/settings.json'), 'utf8');
    // The old name (without the trailing "s") must not appear
    expect(raw).not.toMatch(/auto-memory-hook\.mjs(?!s)/);
  });

  it('does not contain an absolute /docs path', () => {
    const adrDir = settings.claudeFlow?.adr?.directory;
    const dddDir = settings.claudeFlow?.ddd?.directory;
    expect(adrDir).not.toMatch(/^\//);
    expect(dddDir).not.toMatch(/^\//);
  });

  it('does not contain a statusLine block', () => {
    expect(settings).not.toHaveProperty('statusLine');
  });

  it('has correct ADR and DDD directory values', () => {
    expect(settings.claudeFlow.adr.directory).toBe('docs/adr');
    expect(settings.claudeFlow.ddd.directory).toBe('docs/ddd');
  });

  it('has mcp__context7__* and mcp__playwright__* in allow list', () => {
    const allow: string[] = settings.permissions?.allow ?? [];
    expect(allow).toContain('mcp__context7__*');
    expect(allow).toContain('mcp__playwright__*');
  });

  it('every hook command references a file that exists', () => {
    const hooks: Record<string, Array<{ hooks: Array<{ type: string; command?: string }> }>> =
      settings.hooks ?? {};

    for (const [event, groups] of Object.entries(hooks)) {
      for (const group of groups) {
        for (const hook of group.hooks ?? []) {
          if (hook.type !== 'command' || !hook.command) continue;

          // Extract node-invoked files: node "...path..."
          const nodeMatch = hook.command.match(/node\s+"([^"]+)"/);
          if (nodeMatch) {
            const rawPath = nodeMatch[1]
              .replace('$CLAUDE_PROJECT_DIR', ROOT)
              .replace(/"/g, '');
            const abs = resolve(rawPath);
            expect(
              existsSync(abs),
              `Hook ${event} references missing file: ${rawPath}`
            ).toBe(true);
          }
        }
      }
    }
  });
});

// ── .mcp.json ─────────────────────────────────────────────────────────────────

describe('.mcp.json', () => {
  const mcp = readJSON('.mcp.json');

  it('is valid JSON with mcpServers key', () => {
    expect(mcp).toHaveProperty('mcpServers');
    expect(typeof mcp.mcpServers).toBe('object');
  });

  it('defines claude-flow server', () => {
    expect(mcp.mcpServers).toHaveProperty('claude-flow');
    const cf = mcp.mcpServers['claude-flow'];
    expect(cf.command).toBe('npx');
    expect(cf.args).toContain('ruflo@latest');
  });

  it('defines ruv-swarm server', () => {
    expect(mcp.mcpServers).toHaveProperty('ruv-swarm');
  });

  it('defines context7 server', () => {
    expect(mcp.mcpServers).toHaveProperty('context7');
  });

  it('defines playwright server', () => {
    expect(mcp.mcpServers).toHaveProperty('playwright');
  });

  it('has no server with a hard-coded absolute path in args', () => {
    for (const [name, server] of Object.entries(mcp.mcpServers) as Array<[string, { args?: string[] }]>) {
      for (const arg of server.args ?? []) {
        expect(
          arg.startsWith('/workspaces'),
          `${name} args should not contain hard-coded /workspaces path: ${arg}`
        ).toBe(false);
      }
    }
  });
});

// ── docs structure ────────────────────────────────────────────────────────────

describe('docs directory structure', () => {
  const ADR_IDS = ['001','002','003','004','005','006','007','008','009','010'];
  const DDD_DOMAINS = [
    'agent-lifecycle', 'task-execution', 'memory-management',
    'coordination', 'shared-kernel',
  ];

  for (const id of ADR_IDS) {
    it(`docs/adr/ADR-${id}.md exists`, () => {
      expect(existsSync(join(ROOT, `docs/adr/ADR-${id}.md`))).toBe(true);
    });
  }

  it('docs/adr/README.md exists', () => {
    expect(existsSync(join(ROOT, 'docs/adr/README.md'))).toBe(true);
  });

  for (const domain of DDD_DOMAINS) {
    it(`docs/ddd/${domain}.md exists`, () => {
      expect(existsSync(join(ROOT, `docs/ddd/${domain}.md`))).toBe(true);
    });
  }

  it('docs/ddd/README.md exists', () => {
    expect(existsSync(join(ROOT, 'docs/ddd/README.md'))).toBe(true);
  });
});

// ── .claude/commands structure ────────────────────────────────────────────────

describe('.claude/commands structure', () => {
  const EXPECTED_DIRS = [
    '.claude/commands',
    '.claude/commands/agents',
    '.claude/commands/swarm',
    '.claude/commands/memory',
    '.claude/commands/hooks',
    '.claude/commands/github',
    '.claude/commands/coordination',
    '.claude/commands/optimization',
    '.claude/commands/analysis',
  ];

  for (const dir of EXPECTED_DIRS) {
    it(`${dir}/ exists`, () => {
      expect(existsSync(join(ROOT, dir))).toBe(true);
    });
  }

  const EXPECTED_ROOT_COMMANDS = [
    '.claude/commands/claude-flow-help.md',
    '.claude/commands/claude-flow-memory.md',
    '.claude/commands/overview.md',
  ];

  for (const file of EXPECTED_ROOT_COMMANDS) {
    it(`${file} exists`, () => {
      expect(existsSync(join(ROOT, file))).toBe(true);
    });
  }
});
