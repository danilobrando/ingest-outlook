#!/usr/bin/env bash
# install.sh — One-command installer for ingest-outlook.
#
# Usage (from inside ~/second-brain/connectors/ingest-outlook/):
#   ./install.sh
#
# Pre-requirement: clone this repo to ~/second-brain/connectors/ingest-outlook
#   cd ~/second-brain/connectors
#   git clone https://github.com/danilobrando/ingest-outlook.git
#   cd ingest-outlook
#   ./install.sh
#
# What it does:
#   1. Verifies it is running from inside ~/second-brain/connectors/<name>/
#      (the canonical location; see ~/second-brain/connectors/README.md)
#   2. Verifies Python 3.10+ is available
#   3. Symlinks ~/.claude/skills/<name> -> the current directory
#      (Claude Code discovers skills via ~/.claude/skills/; we keep the files
#      in the vault and let CC find them through the symlink)
#   4. Creates ~/.config/<name>/ at mode 0700 for the OAuth token cache
#   5. Smoke-tests fetch.py
#   6. Prints the remaining manual configuration steps
#
# Idempotent: safe to re-run. Will overwrite any existing symlink and recreate
# config dir without touching tokens.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONNECTOR_NAME="$(basename "$SCRIPT_DIR")"
EXPECTED_PARENT="$HOME/second-brain/connectors"
SKILL_SYMLINK="$HOME/.claude/skills/$CONNECTOR_NAME"
CONFIG_DIR="$HOME/.config/$CONNECTOR_NAME"

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

# --- Step 1: Verify location matches the standard ---
step "Verifying install location"
if [[ "$(dirname "$SCRIPT_DIR")" != "$EXPECTED_PARENT" ]]; then
    fail "$(cat <<EOF

Standard violation: this connector must live under
  $EXPECTED_PARENT/

Currently running from:
  $SCRIPT_DIR

To fix, move (or re-clone) the repo to the canonical location:
  mkdir -p $EXPECTED_PARENT
  mv "$SCRIPT_DIR" "$EXPECTED_PARENT/$CONNECTOR_NAME"
  cd "$EXPECTED_PARENT/$CONNECTOR_NAME"
  ./install.sh

(See $HOME/second-brain/connectors/README.md for the connector standard.)
EOF
)"
fi
ok "Running from $SCRIPT_DIR"

# --- Step 2: Verify Python version ---
step "Verifying Python 3.10+"
if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 not found in PATH. Install Python 3.10+ and retry."
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    fail "Python $(python3 --version 2>&1 | awk '{print $2}') is too old. Need 3.10+."
fi
ok "Python $(python3 --version 2>&1 | awk '{print $2}')"

# --- Step 3: Verify source files are present ---
step "Verifying source files"
for f in SKILL.md fetch.py ingest.py .env.example; do
    if [[ ! -f "$SCRIPT_DIR/$f" ]]; then
        fail "Missing source file: $SCRIPT_DIR/$f. Did the clone complete cleanly?"
    fi
done
ok "All source files present"

# --- Step 4: Create symlink from ~/.claude/skills/ ---
step "Creating symlink at $SKILL_SYMLINK"
mkdir -p "$(dirname "$SKILL_SYMLINK")"
if [[ -L "$SKILL_SYMLINK" ]]; then
    existing_target="$(readlink "$SKILL_SYMLINK")"
    if [[ "$existing_target" == "$SCRIPT_DIR" ]]; then
        ok "Symlink already points here (no change)"
    else
        warn "Symlink existed pointing to $existing_target; replacing"
        rm "$SKILL_SYMLINK"
        ln -s "$SCRIPT_DIR" "$SKILL_SYMLINK"
        ok "Symlink updated"
    fi
elif [[ -e "$SKILL_SYMLINK" ]]; then
    warn "A directory or file exists at $SKILL_SYMLINK (not a symlink). Backing up to ${SKILL_SYMLINK}.bak"
    mv "$SKILL_SYMLINK" "${SKILL_SYMLINK}.bak"
    ln -s "$SCRIPT_DIR" "$SKILL_SYMLINK"
    ok "Symlink created; previous installation backed up to ${SKILL_SYMLINK}.bak"
else
    ln -s "$SCRIPT_DIR" "$SKILL_SYMLINK"
    ok "Symlink created"
fi

# --- Step 5: Create config directory ---
step "Preparing config directory $CONFIG_DIR"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
ok "Config dir ready (mode 0700)"

# --- Step 6: Smoke test ---
step "Smoke testing fetch.py through the symlink"
if python3 "$SKILL_SYMLINK/fetch.py" version >/dev/null 2>&1; then
    INSTALLED_VERSION="$(python3 "$SKILL_SYMLINK/fetch.py" version 2>/dev/null | awk '{print $NF}')"
    ok "fetch.py works through symlink ($INSTALLED_VERSION)"
else
    fail "fetch.py smoke test failed. Run: python3 $SKILL_SYMLINK/fetch.py version"
fi

# --- Done. Print next steps ---
printf "\n"
printf "Connector installed at:\n"
printf "  $SCRIPT_DIR  (real files)\n"
printf "  $SKILL_SYMLINK  (symlink for Claude Code discovery)\n"
printf "  $CONFIG_DIR  (token + logs)\n\n"
printf "Three things left, all manual:\n\n"
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
printf "  add the SessionStart hook to ~/.claude/settings.json. See the README\n"
printf "  \"Proactive auto-recovery\" section.\n\n"
printf "Questions? README.md and docs/ have everything. When in doubt: fetch.py fix.\n"
