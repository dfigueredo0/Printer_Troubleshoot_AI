#!/bin/bash
# validate-config.sh
# Runs substantive framework validation: structure checks, JSON validity,
# hook smoke-tests, ADR presence, and an optional quick Vitest pass.
#
# Usage:
#   bash .claude/helpers/validate-config.sh          # full check
#   bash .claude/helpers/validate-config.sh --quick  # skip Vitest
#   bash .claude/helpers/validate-config.sh --ci     # exit 1 on any error

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

MODE="${1:-}"
ERRORS=0
WARNINGS=0

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

pass()  { echo -e "  ${GREEN}✓${NC}  $*"; }
fail()  { echo -e "  ${RED}✗${NC}  $*"; ((ERRORS++)) || true; }
warn()  { echo -e "  ${YELLOW}⚠${NC}  $*"; ((WARNINGS++)) || true; }
info()  { echo -e "  ${BLUE}→${NC}  $*"; }
title() { echo -e "\n${BOLD}$*${NC}"; }

# ── 1. Required files ─────────────────────────────────────────────────────────
title "1. Required files"
REQUIRED_FILES=(
  ".claude/settings.json"
  ".claude/helpers/hook-handler.cjs"
  ".claude/helpers/auto-memory-hooks.mjs"
  ".claude/helpers/swarm-hooks.sh"
  ".claude/helpers/adr-compliance.sh"
  ".claude/helpers/ddd-tracker.sh"
  ".claude/helpers/security-scanner.sh"
  ".mcp.json"
  "docs/adr/README.md"
  "docs/ddd/README.md"
  "package.json"
  "vitest.config.ts"
  "tsconfig.json"
)
for f in "${REQUIRED_FILES[@]}"; do
  [ -f "$f" ] && pass "$f" || fail "$f"
done

# ── 2. Required directories ───────────────────────────────────────────────────
title "2. Required directories"
REQUIRED_DIRS=(
  ".claude/commands/swarm"
  ".claude/commands/memory"
  ".claude/commands/hooks"
  ".claude/commands/github"
  ".claude/commands/coordination"
  ".claude/commands/optimization"
  ".claude/commands/analysis"
  ".claude/commands/agents"
  ".claude/agents"
  "docs/adr"
  "docs/ddd"
)
for d in "${REQUIRED_DIRS[@]}"; do
  [ -d "$d" ] && pass "$d/" || fail "$d/"
done

# ── 3. JSON file validity ─────────────────────────────────────────────────────
title "3. JSON validity"
for json_file in ".claude/settings.json" ".mcp.json"; do
  if [ -f "$json_file" ]; then
    if python3 -c "import json,sys; json.load(open('$json_file'))" 2>/dev/null; then
      pass "$json_file is valid JSON"
    else
      fail "$json_file contains invalid JSON"
    fi
  fi
done

# ── 4. settings.json sanity checks ───────────────────────────────────────────
title "4. settings.json sanity"
python3 << 'PYEOF'
import json, sys, os

try:
    s = json.load(open('.claude/settings.json'))
except Exception as e:
    print(f"  \033[0;31m✗\033[0m  Cannot parse settings.json: {e}")
    sys.exit(0)

checks = [
    (s.get('env', {}).get('CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS') == '1',
     "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 present"),
    ('statusLine' not in s,
     "No stale statusLine block"),
    (not any('auto-memory-hook.mjs"' in str(h.get('command',''))
             for ev, gs in s.get('hooks',{}).items()
             for g in gs for h in g.get('hooks',[])),
     "auto-memory-hooks.mjs filename is correct (no missing 's')"),
    (s.get('claudeFlow',{}).get('adr',{}).get('directory','').startswith('/') == False
     and len(s.get('claudeFlow',{}).get('adr',{}).get('directory','')) > 0,
     "adr.directory is a relative path"),
    (all(p in s.get('permissions',{}).get('allow',[])
         for p in ['mcp__claude-flow__*','mcp__ruv-swarm__*','mcp__context7__*','mcp__playwright__*']),
     "All 4 MCP namespaces in permissions.allow"),
]

ok_sym = '\033[0;32m✓\033[0m'
fail_sym = '\033[0;31m✗\033[0m'
for passed, label in checks:
    print(f"  {ok_sym if passed else fail_sym}  {label}")
    if not passed:
        sys.exit(1)
PYEOF

# ── 5. Hook helper files exist ────────────────────────────────────────────────
title "5. Hook helper file references"
python3 << 'PYEOF'
import json, os, re

s = json.load(open('.claude/settings.json'))
ok_sym = '\033[0;32m✓\033[0m'
fail_sym = '\033[0;31m✗\033[0m'
errors = 0
seen = set()
for event, groups in s.get('hooks', {}).items():
    for group in groups:
        for hook in group.get('hooks', []):
            cmd = hook.get('command', '')
            m = re.search(r'CLAUDE_PROJECT_DIR[^"]*\/helpers\/([^\s"]+)', cmd)
            if m:
                helper = f".claude/helpers/{m.group(1)}"
                if helper not in seen:
                    seen.add(helper)
                    if os.path.exists(helper):
                        print(f"  {ok_sym}  {helper}")
                    else:
                        print(f"  {fail_sym}  {helper}  ← MISSING")
                        errors += 1
import sys
if errors:
    sys.exit(1)
PYEOF

# ── 6. ADR documents ──────────────────────────────────────────────────────────
title "6. ADR documents (ADR-001 through ADR-010)"
adr_ok=true
for i in 001 002 003 004 005 006 007 008 009 010; do
  f="docs/adr/ADR-${i}.md"
  if [ -f "$f" ]; then
    pass "$f"
  else
    fail "$f"
    adr_ok=false
  fi
done

# ── 7. DDD domain documents ───────────────────────────────────────────────────
title "7. DDD domain documents"
for domain in agent-lifecycle task-execution memory-management coordination shared-kernel; do
  f="docs/ddd/${domain}.md"
  [ -f "$f" ] && pass "$f" || fail "$f"
done

# ── 8. Command layer population ───────────────────────────────────────────────
title "8. Command layer (.claude/commands/)"
for dir in swarm memory hooks github coordination optimization analysis agents; do
  full=".claude/commands/$dir"
  if [ -d "$full" ]; then
    count=$(find "$full" -maxdepth 1 -name "*.md" | wc -l)
    if [ "$count" -gt 0 ]; then
      pass "$full/ ($count .md files)"
    else
      warn "$full/ exists but is empty — run apply-commands-migration.sh"
    fi
  else
    fail "$full/ missing"
  fi
done

# ── 9. Node.js environment ────────────────────────────────────────────────────
title "9. Node.js environment"
if command -v node &>/dev/null; then
  VER=$(node --version)
  MAJOR=$(echo "$VER" | cut -d. -f1 | tr -d v)
  [ "$MAJOR" -ge 20 ] && pass "Node.js $VER (≥20)" || fail "Node.js $VER — need ≥20"
else
  fail "Node.js not installed"
fi

# ── 10. Quick hook smoke-test ─────────────────────────────────────────────────
title "10. Hook handler smoke-test"
if command -v node &>/dev/null && [ -f ".claude/helpers/hook-handler.cjs" ]; then
  DIR=$(mktemp -d)
  EXIT_CODE=0
  CLAUDE_PROJECT_DIR="$DIR" node .claude/helpers/hook-handler.cjs route 2>/dev/null || EXIT_CODE=$?
  rm -rf "$DIR"
  [ "$EXIT_CODE" -eq 0 ] && pass "hook-handler.cjs route → exit 0" || fail "hook-handler.cjs route → exit $EXIT_CODE"

  DIR=$(mktemp -d)
  EXIT_CODE=0
  CLAUDE_PROJECT_DIR="$DIR" node .claude/helpers/hook-handler.cjs session-restore 2>/dev/null || EXIT_CODE=$?
  rm -rf "$DIR"
  [ "$EXIT_CODE" -eq 0 ] && pass "hook-handler.cjs session-restore → exit 0" || fail "hook-handler.cjs session-restore → exit $EXIT_CODE"
else
  warn "Skipping hook smoke-test (node or hook-handler.cjs not available)"
fi

# ── 11. Vitest (optional) ─────────────────────────────────────────────────────
if [[ "$MODE" != "--quick" ]] && command -v npx &>/dev/null && [ -f "vitest.config.ts" ]; then
  title "11. Vitest e2e suite"
  if npm run test:e2e -- --reporter=verbose 2>&1 | tail -5; then
    pass "Vitest e2e tests passed"
  else
    fail "Vitest e2e tests failed — run: npm run test:e2e"
  fi
else
  title "11. Vitest (skipped — use npm run test:e2e)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ "$ERRORS" -eq 0 ] && [ "$WARNINGS" -eq 0 ]; then
  echo -e "${GREEN}${BOLD}All checks passed.${NC}"
elif [ "$ERRORS" -eq 0 ]; then
  echo -e "${YELLOW}${BOLD}$WARNINGS warning(s) — no blocking errors.${NC}"
else
  echo -e "${RED}${BOLD}$ERRORS error(s), $WARNINGS warning(s).${NC}"
fi
echo ""

[[ "$MODE" == "--ci" ]] && [ "$ERRORS" -gt 0 ] && exit 1
exit 0
