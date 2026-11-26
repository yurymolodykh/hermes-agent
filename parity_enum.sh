#!/bin/bash
# Per-test outcome enumeration for the parity file, in the state given by $1 (fix|base).
set -u
cd /home/hermes/.hermes/hermes-agent/.worktrees/t_5eb346eb || exit 1
PY=/home/hermes/t72venv/bin/python
STATE=$1
cp hermes_cli/web_routers/webapp.py /tmp/webapp_current.py
if [ "$STATE" = "base" ]; then
  git show da0f60614069:hermes_cli/web_routers/webapp.py > hermes_cli/web_routers/webapp.py
fi
P=0; F=0; E=0
for t in $(grep -oE "^(def|async def) test_[a-zA-Z_0-9]+" tests/hermes_cli/test_webapp_socket_tickets.py | sed -E 's/^(async )?def //'); do
  timeout 90 $PY -m pytest "tests/hermes_cli/test_webapp_socket_tickets.py::$t" -q --no-header -p no:cacheprovider > /tmp/one.log 2>&1
  last=$(grep -E "^[0-9]+ (passed|failed|error)" /tmp/one.log | tail -1)
  p=$(echo "$last" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+"); p=${p:-0}
  f=$(echo "$last" | grep -oE "[0-9]+ failed" | grep -oE "[0-9]+"); f=${f:-0}
  e=$(echo "$last" | grep -oE "[0-9]+ errors?" | grep -oE "[0-9]+"); e=${e:-0}
  P=$((P+p)); F=$((F+f)); E=$((E+e))
  echo "  $t -> passed=$p failed=$f errors=$e"
done
cp /tmp/webapp_current.py hermes_cli/web_routers/webapp.py
echo "TOTAL[$STATE]: passed=$P failed=$F errors=$E"
