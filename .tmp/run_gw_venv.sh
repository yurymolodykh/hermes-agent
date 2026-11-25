#!/usr/bin/env bash
# Local gateway for the py-spy check of card t_acf98596 (see .tmp/run_gw.sh).
set -u
unset HERMES_DELEGATED_CHILD_CONTEXT HERMES_KANBAN_TASK HERMES_KANBAN_WORKSPACE \
      HERMES_KANBAN_WORKSPACES_ROOT HERMES_KANBAN_DB HERMES_KANBAN_BOARD 2>/dev/null || true
export HERMES_HOME=/tmp/gh-t_acf98596
cd /tmp
exec /home/hermes/.hermes/hermes-agent/.venv/bin/python -c "import sys; sys.path.insert(0, '/home/hermes/.hermes/hermes-agent/.worktrees/t_acf98596'); from hermes_cli.main import main; sys.argv=['hermes','gateway','run']; sys.exit(main())"
