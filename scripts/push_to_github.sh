#!/usr/bin/env bash
# push_to_github.sh — one-command push of the extractor/ folder to GitHub.
#
# What it does:
#   1. Initializes git if needed.
#   2. Sets remote `origin` to the URL passed in (or the default below).
#   3. Pre-flight scan: refuses to push if anything sensitive (.env, real PHI,
#      local_runs/, .venv/, etc.) is staged.
#   4. Adds everything respecting .gitignore.
#   5. Commits with a sensible default message (overridable).
#   6. Pushes to `main` (creating the branch if needed).
#
# Auth: uses your existing git credential helper. If the push prompts for
# a username/password, paste a GitHub personal access token (PAT) as the
# password — GitHub no longer accepts passwords for git over HTTPS.
#   PAT setup: https://github.com/settings/tokens (scope: `repo`).
#
# Usage:
#   bash scripts/push_to_github.sh
#   bash scripts/push_to_github.sh "Custom commit message"
#   REMOTE_URL=git@github.com:anusha0695/auto-evaluator-extractor.git bash scripts/push_to_github.sh

set -uo pipefail

DEFAULT_REMOTE="https://github.com/anusha0695/auto-evaluator-extractor.git"
REMOTE_URL="${REMOTE_URL:-$DEFAULT_REMOTE}"
COMMIT_MSG="${1:-Phase 1 — Foundation: schema-driven multi-agent extractor pipeline (87% on demo.pdf)}"

cd "$(dirname "${BASH_SOURCE[0]}")/.."   # → repo root (extractor/)

b_yel() { printf "\n\033[1;33m== %s\033[0m\n" "$*"; }
b_grn() { printf "\033[1;32m✓ %s\033[0m\n" "$*"; }
b_red() { printf "\033[1;31m✗ %s\033[0m\n" "$*"; }
b_inf() { printf "  %s\n" "$*"; }

# ---------------------------------------------------------------------------
# 1. Git init
# ---------------------------------------------------------------------------
b_yel "Step 1 — git init (if needed)"
if [[ -d .git ]]; then
  b_grn "git already initialized"
else
  git init -q
  git branch -M main
  b_grn ".git initialized, default branch set to main"
fi

# ---------------------------------------------------------------------------
# 2. Remote setup
# ---------------------------------------------------------------------------
b_yel "Step 2 — remote configuration"
if git remote get-url origin >/dev/null 2>&1; then
  current=$(git remote get-url origin)
  if [[ "$current" == "$REMOTE_URL" ]]; then
    b_grn "remote 'origin' already set to $REMOTE_URL"
  else
    b_inf "remote 'origin' currently: $current"
    b_inf "updating to: $REMOTE_URL"
    git remote set-url origin "$REMOTE_URL"
    b_grn "remote updated"
  fi
else
  git remote add origin "$REMOTE_URL"
  b_grn "remote 'origin' added: $REMOTE_URL"
fi

# ---------------------------------------------------------------------------
# 3. Pre-flight scan — refuse to push anything sensitive
# ---------------------------------------------------------------------------
b_yel "Step 3 — pre-flight safety scan"

# Refresh the index so .gitignore changes take effect on previously-tracked files.
git rm -r --cached --ignore-unmatch -q .env local_runs/ prod_prompt_data/ setup.log .venv/ 2>/dev/null || true
git add -A

# Enumerate everything that would be in the next commit
staged=$(git diff --cached --name-only)
if [[ -z "$staged" ]]; then
  b_inf "Nothing staged. Either everything is committed already, or .gitignore is excluding everything."
fi

# Patterns that absolutely must NOT appear in $staged
declare -a forbidden=(
  "^\\.env$"
  "^\\.venv/"
  "^local_runs/"
  "^prod_prompt_data/"
  "^setup\\.log$"
  "service-account.*\\.json$"
  "^.*-key.*\\.json$"
  "\\.pem$"
)

violations=0
for pat in "${forbidden[@]}"; do
  matches=$(echo "$staged" | grep -E "$pat" || true)
  if [[ -n "$matches" ]]; then
    b_red "FORBIDDEN file(s) staged matching /$pat/:"
    echo "$matches" | sed 's/^/      /'
    violations=$((violations + 1))
  fi
done

if [[ $violations -gt 0 ]]; then
  b_red "$violations violation(s) found. Aborting push."
  b_inf "Fix .gitignore and re-run, or manually 'git rm --cached <file>'."
  exit 1
fi
b_grn "no forbidden files in staging area"

# Show what WILL be pushed (compact summary)
n_files=$(echo "$staged" | grep -c '^' || true)
b_inf "$n_files file(s) staged for commit"
echo "$staged" | head -15 | sed 's/^/      /'
if [[ $n_files -gt 15 ]]; then
  echo "      ... (+$((n_files - 15)) more)"
fi

# ---------------------------------------------------------------------------
# 4. Confirm + commit + push
# ---------------------------------------------------------------------------
b_yel "Step 4 — commit + push"
b_inf "Commit message: $COMMIT_MSG"
b_inf "Remote:         $REMOTE_URL"
b_inf "Branch:         main"
echo
read -r -p "Proceed? [y/N] " ans
if [[ ! "$ans" =~ ^[Yy]$ ]]; then
  b_inf "Aborted by user. Nothing pushed."
  exit 0
fi

# Author info — only set if globally unset (don't override existing config)
if ! git config user.email >/dev/null 2>&1; then
  git config user.email "anusha@local.dev"
  git config user.name "Anusha"
fi

if [[ -n "$staged" ]]; then
  git commit -q -m "$COMMIT_MSG"
  b_grn "committed"
else
  b_inf "nothing to commit (working tree clean)"
fi

b_inf "pushing to $REMOTE_URL ..."
if git push -u origin main; then
  b_grn "Push succeeded."
  echo
  b_inf "View the repo: ${REMOTE_URL%.git}"
else
  b_red "Push failed."
  b_inf "Common fixes:"
  b_inf "  - Auth: use a GitHub personal access token (PAT) as the password"
  b_inf "    https://github.com/settings/tokens   scope: repo"
  b_inf "  - First push to an existing repo with content: try"
  b_inf "    git pull --rebase origin main   then re-run this script"
  exit 1
fi
