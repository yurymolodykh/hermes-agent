#!/usr/bin/env bash
# Run the step-4 tests against a copy of the tree outside the real home.
# The worktree lives INSIDE ~/.hermes, where tests/home_io_guard.py refuses DB init
# (it also refuses the base tree), so the copy at /tmp/mpd4/repo5 with an isolated
# HERMES_HOME is what the suite must see. aiohttp/pytest-asyncio live in the
# install environment's site-packages / a copied dep dir, not in the venv.
set -u
BASE="$HOME/.hermes"
VENV_PY="$BASE/hermes-agent/.venv/bin/python"
INSTALL_SP=$(ls -d "$BASE"/installs/*/environments/*/venv/lib/python3.14/site-packages 2>/dev/null | head -1)
export PYTHONPATH="$INSTALL_SP:/tmp/mpd4/deps"
export TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0
unset OPENAI_API_KEY ANTHROPIC_API_KEY TELEGRAM_BOT_TOKEN 2>/dev/null || true
REPO="${MPD_REPO:-/tmp/mpd4/repo5}"
export HERMES_HOME="$(mktemp -d /tmp/mpd4/home-XXXXXX)"
cd "$REPO" || exit 1
exec "$VENV_PY" -m pytest -q --no-header -p no:cacheprovider "$@"
