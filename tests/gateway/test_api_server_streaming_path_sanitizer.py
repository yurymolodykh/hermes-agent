"""Streaming path-sanitizer regression: fragmented paths must not leak.

The non-stream whole-text ``_strip_local_file_paths`` correctly removes an
EXISTING absolute local path when it sees the complete string at once.  But a
live LLM stream splits one path across many small tokens (``"C"``, ``":/"``,
``"Users"``, ...``".txt"``), and the per-chunk ``delta.content`` cleaner runs on
each token *before* the path is complete — so a per-chunk strip never sees the
whole path and the path leaks into user-visible content.

``_StreamingPathSanitizer`` is the fix: it holds back the stream tail after the
last whitespace/newline, reassembles each candidate path across tokens, and only
emits bytes once a boundary proves the path complete (running the same
whole-text strip on the released prefix).  This module is deliberately
self-contained (imports only the helper).  It was written while
``tests/gateway/test_api_server.py`` could not be collected because of a
missing ``_classify_openwebui_service_request`` symbol; card t_635176e3
removed those tests, so the blocker is gone.

Contract under test:
  * an EXISTING absolute local path, streamed in fragmented tokens, never
    appears (in whole or in part) in the emitted visible content;
  * a NON-existing absolute path stays visible (the model may be discussing it);
  * a path inside a backtick code example stays visible (code samples are
    never mutilated);
  * prose around the path survives unchanged.
"""

import tempfile
from pathlib import Path

import pytest

from gateway.platforms.api_server import _StreamingPathSanitizer


def _feed_all(sanitizer, fragments):
    """Feed fragments through the sanitizer and return the streamed output."""
    out = []
    for frag in fragments:
        chunk = sanitizer.feed(frag)
        if chunk:
            out.append(chunk)
    chunk = sanitizer.flush()
    if chunk:
        out.append(chunk)
    return "".join(out)


@pytest.fixture
def existing_path():
    """Create a REAL file so the sanitizer treats its path as a genuine deliverable."""
    with tempfile.TemporaryDirectory(prefix="streamsan_") as d:
        p = Path(d) / "report_data_123.txt"
        p.write_text("probe", encoding="utf-8")
        yield str(p.resolve())


def _split_n(s, n):
    """Split ``s`` into roughly ``n`` chunks at arbitrary boundaries."""
    k = max(1, len(s) // n)
    return [s[i : i + k] for i in range(0, len(s), k)]


def test_fragmented_existing_path_never_leaks_3char(existing_path):
    # The exact class of fragmentation the old per-chunk loop leaked.
    fragments = [existing_path[i : i + 3] for i in range(0, len(existing_path), 3)]
    out = _feed_all(_StreamingPathSanitizer(), fragments)
    assert out == ""
    joined_all = "".join(fragments)
    assert joined_all not in _feed_all(_StreamingPathSanitizer(), fragments)


def test_fragmented_existing_path_never_leaks_5char(existing_path):
    fragments = [existing_path[i : i + 5] for i in range(0, len(existing_path), 5)]
    out = _feed_all(_StreamingPathSanitizer(), fragments)
    assert out == ""


def test_fragmented_existing_path_never_leaks_arbitrary_chunks(existing_path):
    for n in (4, 7, 12):
        fragments = _split_n(existing_path, n)
        out = _feed_all(_StreamingPathSanitizer(), fragments)
        assert out == "", f"leak with {n} chunks: {out!r}"


def test_prose_surrounding_path_survives(existing_path):
    frags = ["The file is at:\n\n"]
    frags += [existing_path[i : i + 4] for i in range(0, len(existing_path), 4)]
    frags += ["\n\nContents: hello (5 bytes)."]
    out = _feed_all(_StreamingPathSanitizer(), frags)
    assert "The file is at:" in out
    assert "Contents: hello (5 bytes)." in out
    assert existing_path not in out


def test_nonexistent_path_stays_visible():
    # A path that does not exist is NOT a real deliverable; it must survive.
    fake = "C:/Users/nobody/AppData/Local/Temp/does_not_exist_123/report.txt"
    frags = _split_n(fake, 6)
    out = _feed_all(_StreamingPathSanitizer(), frags)
    assert fake in out


def test_backtick_code_path_stays_visible(existing_path):
    # Code samples must never be mutilated, even when token-fragmented.
    frags = ["Use `"]
    frags += [existing_path[i : i + 4] for i in range(0, len(existing_path), 4)]
    frags += ["` verbatim"]
    out = _feed_all(_StreamingPathSanitizer(), frags)
    assert "Use `" in out
    assert "` verbatim" in out


def test_multiple_paths_in_one_stream(existing_path):
    with tempfile.TemporaryDirectory(prefix="streamsan2_") as d2:
        p2 = Path(d2) / "second.txt"
        p2.write_text("two", encoding="utf-8")
        p2s = str(p2.resolve())
        frags = ["First: "]
        frags += [existing_path[i : i + 3] for i in range(0, len(existing_path), 3)]
        frags += [" and second: "]
        frags += [p2s[i : i + 3] for i in range(0, len(p2s), 3)]
        frags += [" done."]
        out = _feed_all(_StreamingPathSanitizer(), frags)
        assert existing_path not in out
        assert p2s not in out
        assert "First: " in out
        assert " and second: " in out
        assert " done." in out


def test_whitespace_boundaries_flush_between_paths(existing_path):
    # A completed sentence in between must be emitted on the whitespace
    # boundary, not held back indefinitely.
    frags = ["Hello world. "]
    frags += [existing_path[i : i + 4] for i in range(0, len(existing_path), 4)]
    out = _feed_all(_StreamingPathSanitizer(), frags)
    assert "Hello world. " in out
    assert existing_path not in out
