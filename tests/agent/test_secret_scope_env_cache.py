"""The per-path ``load_env_file()`` memo in ``agent.secret_scope``.

``build_profile_secret_scope()`` sits on the gateway's hot paths (every turn, cron fire, MCP/browser
adoption, housekeeping drain) and used to re-read and re-parse the profile ``.env`` on each call.
A stale credential map is a worse failure than a slow one, so the contract under test is: parse once
while the file is unchanged, hand every caller its own dict, and see every real change on disk.

Files are written as bytes: ``Path.write_text`` translates ``\\n`` to ``\\r\\n`` on Windows, which
would break the same-size assertion.
"""

from __future__ import annotations

import os
import time

import pytest

import agent.secret_scope as ss

# Comfortably past the coarsest mtime resolution in common use (FAT32 truncates to two seconds).
_MTIME_STEP_NS = 10_000_000_000


@pytest.fixture(autouse=True)
def _clear_cache():
    ss.invalidate_env_file_cache()
    yield
    ss.invalidate_env_file_cache()


def _count_parses(monkeypatch) -> list:
    calls: list = []
    real = ss._parse_env_text
    monkeypatch.setattr(ss, "_parse_env_text", lambda t: (calls.append(t), real(t))[1])
    return calls


def _rewrite_same_size(path, text: str) -> None:
    """Same length, different content: only the mtime distinguishes the versions."""
    previous = path.stat()
    path.write_bytes(text.encode("utf-8"))
    assert path.stat().st_size == previous.st_size
    bumped = previous.st_mtime_ns + _MTIME_STEP_NS
    os.utime(path, ns=(bumped, bumped))


def test_unchanged_file_is_parsed_once_and_each_caller_gets_its_own_dict(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_bytes(b"A=1\nB=2\n")
    calls = _count_parses(monkeypatch)
    monkeypatch.setattr("hermes_cli.env_loader.get_secret_source_values", lambda home: {"EXT": "vault"})

    first = ss.load_env_file(env)
    first["INJECTED"] = "must-not-persist"  # callers mutate what they get back
    for _ in range(5):
        assert ss.load_env_file(env) == {"A": "1", "B": "2"}
    # The motivating path layers external secrets onto the result; that must not poison the memo.
    scope = ss.build_profile_secret_scope(tmp_path)
    assert scope["EXT"] == "vault"
    assert ss.load_env_file(env) == {"A": "1", "B": "2"}

    assert len(calls) == 1, f"expected one parse, got {len(calls)}"


def test_edits_and_deletion_are_seen(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_bytes(b"KEY=aaa\n")
    calls = _count_parses(monkeypatch)
    assert ss.load_env_file(env) == {"KEY": "aaa"}

    _rewrite_same_size(env, "KEY=bbb\n")
    assert ss.load_env_file(env) == {"KEY": "bbb"}
    assert len(calls) == 2

    env.unlink()
    assert ss.load_env_file(env) == {}
    env.write_bytes(b"KEY=ccc\n")
    assert ss.load_env_file(env) == {"KEY": "ccc"}
    assert len(calls) == 3


def test_same_size_rewrite_inside_one_timestamp_tick_is_seen(tmp_path, monkeypatch):
    """On a kernel stamping mtime/ctime from the coarse (jiffy) clock, an in-place same-size rewrite
    in the same tick leaves (mtime, size, inode, ctime) unchanged. Simulate that clock: every stat
    identity collides, so only the bytes tell the versions apart (CI run 39146191939, where
    ``reload.env`` reported ``{'updated': 0}`` after ``.env`` went from ``one`` to ``two``).
    """
    monkeypatch.setattr(ss, "file_signature", lambda st: (0, 0, 0, 0), raising=False)
    env = tmp_path / ".env"
    env.write_bytes(b"TUI_RELOAD_PROBE=one\n")
    calls = _count_parses(monkeypatch)
    assert ss.load_env_file(env) == {"TUI_RELOAD_PROBE": "one"}
    assert ss.load_env_file(env) == {"TUI_RELOAD_PROBE": "one"}
    assert len(calls) == 1  # unchanged bytes still skip the parse

    env.write_bytes(b"TUI_RELOAD_PROBE=two\n")
    assert ss.load_env_file(env) == {"TUI_RELOAD_PROBE": "two"}
    assert len(calls) == 2


def _settle(path) -> None:
    """Age the file past ``utils.RACY_MTIME_WINDOW_NS`` so its signature is trusted on its own."""
    import utils
    old = time.time_ns() - (utils.RACY_MTIME_WINDOW_NS * 2)
    os.utime(path, ns=(old, old))


def test_a_settled_hit_does_not_open_the_file(tmp_path, monkeypatch):
    """The whole point of the signature: a settled, unchanged ``.env`` costs one stat, not open+read."""
    import builtins
    import utils

    env = tmp_path / ".env"
    env.write_bytes(b"A=1\nB=2\n")
    _settle(env)
    monkeypatch.setattr(ss, "mtime_is_racy", lambda _mtime_ns: False)

    opens: list[str] = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        opens.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    assert ss.load_env_file(env) == {"A": "1", "B": "2"}
    opened_on_first = len(opens)
    for _ in range(5):
        assert ss.load_env_file(env) == {"A": "1", "B": "2"}
    assert len(opens) == opened_on_first, f"a cache hit opened the file: {opens}"
    monkeypatch.setattr(builtins, "open", real_open)


def test_a_settled_edit_is_visible_on_the_very_next_call(tmp_path, monkeypatch):
    """Settled signature + one append: the next call re-reads and serves the new value immediately."""
    import utils

    env = tmp_path / ".env"
    env.write_bytes(b"KEY=aaa\n")
    _settle(env)
    monkeypatch.setattr(ss, "mtime_is_racy", lambda _mtime_ns: False)
    assert ss.load_env_file(env) == {"KEY": "aaa"}

    with env.open("ab") as fh:  # size changes as well as mtime
        fh.write(b"NEW=bbb\n")
    assert ss.load_env_file(env) == {"KEY": "aaa", "NEW": "bbb"}

    # An atomic replace (new inode) with the SAME size and a restored mtime is still caught: ctime
    # cannot be backdated from user space.
    old = env.stat()
    env.write_bytes(b"KEY=ccc\nNEW=d\x00d\n"[: old.st_size])
    os.utime(env, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert ss.load_env_file(env) != {"KEY": "aaa", "NEW": "bbb"}


def test_deleting_the_file_drops_the_memo_entry(tmp_path):
    """A vanished ``.env`` must not keep serving its secrets from the memo (nor leave a phantom entry)."""
    env = tmp_path / ".env"
    env.write_bytes(b"SECRET=live\n")
    _settle(env)
    assert ss.load_env_file(env) == {"SECRET": "live"}
    assert str(env) in ss._ENV_FILE_CACHE

    env.unlink()
    assert ss.load_env_file(env) == {}
    assert str(env) not in ss._ENV_FILE_CACHE, "a deleted file left its secrets memoised"

    # Recreated with different content: served fresh, not the pre-deletion map.
    env.write_bytes(b"SECRET=other\n")
    assert ss.load_env_file(env) == {"SECRET": "other"}
