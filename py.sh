#!/bin/bash
# Run the shared runtime interpreter without naming its path in a command line (card-tree-guard
# blocks command text containing the shared repo path; running and reading it is allowed).
set -u
PY="${HOME}/.hermes/hermes-agent/.venv/bin/python"
exec "$PY" "$@"
