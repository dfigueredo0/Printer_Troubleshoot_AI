/**
 * Integration tests — MCP server connectivity
 *
 * These tests verify that the configured MCP servers can be started
 * and respond to the MCP initialize handshake. They are skipped in
 * environments where npx is unavailable (e.g. offline CI runners).
 *
 * Run with: npm run test:integration
 */

import { describe, it, expect, beforeAll } from 'vitest';
import { execFile } from 'child_process';
import { promisify } from 'util';

const execAsync = promisify(execFile);

const SERVERS = [
  { name: 'claude-flow', pkg: 'ruflo@latest', args: ['mcp', 'start'] },
  { name: 'ruv-swarm',   pkg: 'ruv-swarm',    args: ['mcp', 'start'] },
];

// MCP initialize request (2024-11-05 protocol)
const INIT_MESSAGE = JSON.stringify({
  jsonrpc: '2.0',
  method: 'initialize',
  params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'test', version: '0.0.1' } },
  id: 1,
}) + '\n';

function pingMcpServer(pkg: string, args: string[]): Promise<boolean> {
  return new Promise((resolve) => {
    const timeout = setTimeout(() => {
      proc.kill();
      resolve(false);
    }, 10_000);

    const proc = require('child_process').spawn('npx', ['-y', pkg, ...args], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    let stdout = '';
    proc.stdout.on('data', (d: Buffer) => {
      stdout += d.toString();
      // MCP servers respond with a JSON result to initialize
      if (stdout.includes('"result"') && stdout.includes('"serverInfo"')) {
        clearTimeout(timeout);
        proc.kill();
        resolve(true);
      }
    });

    proc.on('close', () => {
      clearTimeout(timeout);
      resolve(false);
    });

    proc.stdin.write(INIT_MESSAGE);
  });
}

describe('MCP server connectivity', () => {
  for (const server of SERVERS) {
    it(`${server.name} responds to MCP initialize`, async () => {
      // Skip if no network / npx unavailable
      const npxAvailable = await execAsync('which', ['npx'])
        .then(() => true)
        .catch(() => false);

      if (!npxAvailable) {
        console.log(`Skipping ${server.name}: npx not available`);
        return;
      }

      const alive = await pingMcpServer(server.pkg, server.args);
      expect(alive).toBe(true);
    }, 15_000); // 15s timeout for cold npx starts
  }
});

describe('settings.json MCP configuration', () => {
  it('references only packages that exist on npm', async () => {
    const settings = JSON.parse(
      require('fs').readFileSync('.claude/settings.json', 'utf8')
    );

    const servers = settings.mcpServers ?? {};
    for (const [name, config] of Object.entries(servers as Record<string, { args?: string[] }>)) {
      const pkg = config.args?.[1]; // npx -y <pkg>
      if (!pkg) continue;

      const { stdout } = await execAsync('npm', ['view', pkg, 'name']).catch(() => ({ stdout: '' }));
      expect(stdout.trim(), `MCP server "${name}" references unknown package "${pkg}"`).toBeTruthy();
    }
  });

  it('has no absolute /docs paths in claudeFlow config', () => {
    const settings = JSON.parse(
      require('fs').readFileSync('.claude/settings.json', 'utf8')
    );
    const adrDir = settings.claudeFlow?.adr?.directory ?? '';
    const dddDir = settings.claudeFlow?.ddd?.directory ?? '';

    expect(adrDir.startsWith('/'), `adr.directory should be relative, got "${adrDir}"`).toBe(false);
    expect(dddDir.startsWith('/'), `ddd.directory should be relative, got "${dddDir}"`).toBe(false);
  });

  it('auto-memory-hooks.mjs is referenced with the correct filename', () => {
    const raw = require('fs').readFileSync('.claude/settings.json', 'utf8');
    expect(raw).not.toContain('auto-memory-hook.mjs');
    expect(raw).toContain('auto-memory-hooks.mjs');
  });

  it('statusLine block is absent', () => {
    const settings = JSON.parse(
      require('fs').readFileSync('.claude/settings.json', 'utf8')
    );
    expect(settings).not.toHaveProperty('statusLine');
  });
});
