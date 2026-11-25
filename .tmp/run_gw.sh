#!/usr/bin/env bash
# Local gateway for the py-spy check of card t_acf98596: runs THIS worktree's code on an
# isolated HERMES_HOME and port, so the live WebSocket path can be profiled without touching
# the shared platform tree or the owner's gateway.
set -u
export PYTHONPATH=/tmp/sp314
export HERMES_HOME=/tmp/gh-t_acf98596
exec "$1" -c "import sys; sys.path.insert(0, '/home/hermes/.hermes/hermes-agent/.worktrees/t_acf98596'); from hermes_cli.main import main; sys.argv=['hermes','gateway','run']; sys.exit(main())"
