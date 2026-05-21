#!/usr/bin/env bash
# install.sh — One-command installer for ingest-outlook.
#
# Usage: ./install.sh
#
# What it does:
#   1. Verifies Python 3.10+ is available
#   2. Copies SKILL.md, fetch.py, ingest.py, .env.example to ~/.claude/skills/ingest-outlook/
#   3. Creates ~/.config/ingest-outlook/ at mode 0700 for the OAuth token cache
#   4. Prints the remaining manual configuration steps (env vars + Azure app)
#
# Idempotent: safe to re-run. Overwrites existing skill files (use to upgrade).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$HOME/.claude/skills/ingest-outlook"
CONFIG_DIR="$HOME/.config/ingest-outlook"

# --- Pretty output helpers ---
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
RED=$'\033[0;31m'
RESET=$'\033[0m'
ok()    { printf "%s[ok]%s %s\n" "$GREEN" "$RESET" "$1"; }
warn()  { printf "%s[!!]%s %s\n" "$YELLOW" "$RESET" "$1"; }
fail()  { printf "%s[!!]%s %s\n" "$RED" "$RESET" "$1"; exit 1; }
step()  { printf "\n  -> %s\n" "$1"; }

printf "ingest-outlook installer\n"
printf "========================\n"

# --- Step 1: Verify Python version ---
step "Verifying Python 3.10+"
if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 not found in PATH. Install Python 3.10+ and retry."
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    fail "Python $(python3 --version 2>&1 | awk '{print $2}') is too old. Need 3.10+."
fi
ok "Python $(python3 --version 2>&1 | awk '{print $2}')"

# --- Step 2: Verify source files are present ---
step "Verifying installer source files"
for f in SKILL.md fetch.py ingest.py .env.example; do
    if [[ ! -f "$SCRIPT_DIR/$f" ]]; then
        fail "Missing source file: $SCRIPT_DIR/$f. Did you clone the full repo?"
    fi
done
ok "All source files present"

# --- Step 3: Install skill files ---
step "Installing skill to $SKILL_DIR"
mkdir -p "$SKILL_DIR"
cp "$SCRIPT_DIR/SKILL.md" "$SCRIPT_DIR/fetch.py" "$SCRIPT_DIR/ingest.py" "$SCRIPT_DIR/.env.example" "$SKILL_DIR/"
ok "Copied 4 files to $SKILL_DIR"

# --- Step 4: Create config directory ---
step "Preparing config directory $CONFIG_DIR"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
ok "Config dir ready (mode 0700)"

# --- Step 5: Smoke test ---
step "Smoke testing fetch.py"
if python3 "$SKILL_DIR/fetch.py" version >/dev/null 2>&1; then
    INSTALLED_VERSION="$(python3 "$SKILL_DIR/fetch.py" version 2>/dev/null | awk '{print $NF}')"
    ok "fetch.py works ($INSTALLED_VERSION)"
else
    fail "fetch.py smoke test failed. Run: python3 $SKILL_DIR/fetch.py version"
fi

# --- Done. Print next steps ---
printf "\n"
printf "Skill files installed. Three things left, all manual:\n\n"
printf "  1) Register an Azure app (one-time, ~5 minutes).\n"
printf "     Full walkthrough: docs/azure-app-setup.md\n"
printf "     You will get back two GUIDs: a Client ID and a Tenant ID.\n\n"
printf "  2) Add the env vars to your shell. In ~/.zshrc (or ~/.bashrc), append:\n\n"
printf "       export MS_GRAPH_CLIENT_ID=\"<your-application-client-id>\"\n"
printf "       export MS_GRAPH_TENANT_ID=\"common\"   # or your tenant UUID for corporate\n\n"
printf "     Then: source ~/.zshrc\n\n"
printf "  3) Run the diagnose-and-fix command. First run opens a browser for OAuth consent:\n\n"
printf "       python3 ~/.claude/skills/ingest-outlook/fetch.py fix\n\n"
printf "  (Optional) For autonomous self-healing on every Claude Code session start,\n"
printf "  add the SessionStart hook to ~/.claude/settings.json. See the README \"Proactive\n"
printf "  auto-recovery\" section.\n\n"
printf "Questions? README.md and docs/ have everything. When in doubt: fetch.py fix.\n"
