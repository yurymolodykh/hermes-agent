"""Probe: the resolve cost of the roster's name lookup, isolated for py-spy.

Run as ``python probe_roster_resolve.py <before|after>``. Both variants execute the same call
the WS stack makes per served bot (``profile_name_for_home`` on each profile home, in a loop);
``before`` disables the memo the way the old code did (no hit ever), ``after`` runs the shipped
memoization. py-spy sampling the two processes shows whether ``realpath`` is still on the path.
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, '/home/hermes/.hermes/hermes-agent/.worktrees/t_acf98596')

mode = sys.argv[1] if len(sys.argv) > 1 else 'after'
root = Path(tempfile.mkdtemp(prefix=f'probe-{mode}-'))
(root / 'config.yaml').write_text('{}')
homes = [root]
for name in ('alpha', 'beta', 'gamma', 'delta'):
    home = root / 'profiles' / name
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text('{}')
    homes.append(home)

import hermes_constants as hc  # noqa: E402

if mode == 'before':
    hc._PROFILE_NAME_TTL = -1.0          # never a hit: every call resolves, as before the fix
else:
    hc._PROFILE_NAME_TTL = 60.0

calls = 0
deadline = time.monotonic() + 12


def worker():
    global calls
    local = 0
    while time.monotonic() < deadline:
        for home in homes:
            hc.profile_name_for_home(home)
            local += 1
    calls += local


threads = [threading.Thread(target=worker) for _ in range(4)]
start = time.monotonic()
for t in threads:
    t.start()
for t in threads:
    t.join()
elapsed = time.monotonic() - start
print(f'{mode}: {calls} lookups in {elapsed:.1f}s = {calls / elapsed:,.0f}/s')
