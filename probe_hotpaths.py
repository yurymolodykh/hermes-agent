"""Per-operation cost of the three t_77d98767 hotspots (base vs patched tree).

The acceptance number comes from py-spy on /api/status, but on an idle test stand those three
frames sit below sampling resolution, so this probe measures them directly on the code paths a
request actually runs:

  1. ``build_profile_secret_scope(home)`` — the gateway's per-turn/per-request secret resolution
     (re-reads HERMES_HOME/.env through ``agent.secret_scope.load_env_file``).
  2. ``tools.skills_tool.find_all_skills()`` — the skill catalog listing a dashboard/agent request
     makes (walks the skills tree for ``_dir_tree_signature``).
  3. ``SessionDB.get_session()`` — every read opens/reuses a read connection, which passes
     ``hermes_state_readpool._fd_headroom_ok`` (a /proc/self/fd listing when the memo is cold).

Usage: python probe_hotpaths.py <tree-root> [iterations]
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

TREE = sys.argv[1]
ITERS = int(sys.argv[2]) if len(sys.argv) > 2 else 300
sys.path.insert(0, TREE)

TMP = Path(tempfile.mkdtemp(prefix="t77-hot-"))
HOME = TMP / "home"
SKILLS = HOME / "skills"
HOME.mkdir(parents=True, exist_ok=True)
(HOME / "config.yaml").write_text(
    "model:\n  default: letovo/deepseek-v4-flash\ndatabase:\n  cache_size: -8000\n"
    "  synchronous: FULL\nsessions:\n  auto_archive: false\n", encoding="utf-8")
# A realistic profile .env (the live one measured ~27 KB) and a realistic skills tree
# (170 dirs / 5 categories in the live profile).
(HOME / ".env").write_text(
    "".join(f"KEY_{i:03d}=value-{i}-{'x' * 45}\n" for i in range(420)), encoding="utf-8")
for cat in range(5):
    for sub in range(6):
        for name in range(6):
            d = SKILLS / f"cat{cat}" / f"sub{sub}" / f"skill{name}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(
                f"---\nname: skill{name}-{cat}{sub}-{sub}\ndescription: desc {name}\n---\n# s\n",
                encoding="utf-8")

os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_TEST_ISOLATION"] = "1"
os.environ.setdefault("HERMES_STATE_DB_GUARD_BYPASS", "1")

STATS: dict[str, int] = {}


def _count(label: str) -> None:
    STATS[label] = STATS.get(label, 0) + 1


def timeit(label: str, fn, iters: int) -> dict:
    fn()  # warm
    best = []
    t0 = time.perf_counter()
    for _ in range(iters):
        s = time.perf_counter()
        fn()
        best.append((time.perf_counter() - s) * 1e6)
    total = time.perf_counter() - t0
    best.sort()
    return {"label": label, "iters": iters, "us_per_op_median": round(best[len(best) // 2], 1),
            "us_per_op_p95": round(best[int(len(best) * 0.95)], 1),
            "us_per_op_mean": round(total / iters * 1e6, 1),
            "total_ms": round(total * 1000, 1)}


def main() -> int:
    import agent.secret_scope as ss
    import tools.skills_tool as st

    # Count the underlying work so the timing has a mechanism attached to it.
    real_parse = ss._parse_env_text
    ss._parse_env_text = lambda text: (_count("env_parses"), real_parse(text))[1]
    real_sig = st._dir_tree_signature
    st._dir_tree_signature = lambda *a, **kw: (_count("dir_signature_calls"), real_sig(*a, **kw))

    from hermes_state import SessionDB

    db = SessionDB(db_path=HOME / "state.db")
    db.create_session(session_id="s1", source="cli", model="m")
    db.append_message("s1", role="user", content="hi")

    import hermes_state_readpool as rp
    real_fd = rp._open_fd_count

    def counting_fd():
        _count("fd_listings")
        return real_fd()

    rp._open_fd_count = counting_fd

    results = []
    STATS.clear()
    results.append(timeit("build_profile_secret_scope", lambda: ss.build_profile_secret_scope(HOME), ITERS))
    results[-1]["underlying"] = {k: v for k, v in STATS.items() if k == "env_parses"}
    STATS.clear()
    st.clear_skills_cache()
    results.append(timeit("find_all_skills", lambda: st.find_all_skills(), ITERS))
    results[-1]["underlying"] = {k: v for k, v in STATS.items() if k == "dir_signature_calls"}
    STATS.clear()
    rp._fd_usage_cache = (0.0, None)  # cold memo: the worst case a burst sees
    results.append(timeit("SessionDB.get_session (fd headroom per read)",
                          lambda: db.get_session("s1"), ITERS))
    results[-1]["underlying"] = {"fd_listings": STATS.get("fd_listings", 0)}
    db.close()

    print(json.dumps({"tree": TREE, "iters": ITERS, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
