"""Same loop as probe_roster_resolve.py, never exiting, for py-spy record/dump."""

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, '/home/hermes/.hermes/hermes-agent/.worktrees/t_acf98596')

mode = sys.argv[1] if len(sys.argv) > 1 else 'after'
root = Path(tempfile.mkdtemp(prefix=f'probe-loop-{mode}-'))
(root / 'config.yaml').write_text('{}')
homes = [root]
for name in ('alpha', 'beta', 'gamma', 'delta'):
    home = root / 'profiles' / name
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text('{}')
    homes.append(home)

import hermes_constants as hc  # noqa: E402

hc._PROFILE_NAME_TTL = -1.0 if mode == 'before' else 60.0


def worker():
    while True:
        for home in homes:
            hc.profile_name_for_home(home)


threads = [threading.Thread(target=worker) for _ in range(4)]
for t in threads:
    t.start()
while True:
    time.sleep(60)
