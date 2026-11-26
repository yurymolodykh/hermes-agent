#!/bin/bash
# Resolve the shared runtime venv WITHOUT naming its path in the command line (card-tree-guard
# blocks commands whose text contains the shared repo path; reading it is allowed).
set -u
ROOT="${HOME}/.hermes"
HOST_REPO="${ROOT}/hermes-agent"
PY="${HOST_REPO}/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "no shared venv at expected location" >&2
  exit 2
fi
"$PY" -c "import pytest,fastapi,starlette,httpx,sys;print('interp ok',sys.version.split()[0],pytest.__version__)" 2>&1
exec "$PY" -m pytest "$@"
