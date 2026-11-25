"""Parsing an inline bootstrap must be paid once per command line, not once per status poll.

``_bootstrap_entry`` tries six DOTALL patterns in order and ``inline_bootstrap_argv``
re-joins every token run before that; py-spy attributed 123 of 2049 gateway samples to the
``_bootstrap_entry`` genexpr alone. The parse is a pure function of ``(source, argv)``, so
its result is memoised and a repeat costs a dict lookup.

The memo must never change an answer: these tests pin that a cached call returns exactly
what the parser returns, and that it does no pattern work at all.
"""

from __future__ import annotations

import shlex

import pytest

from gateway import status_inline_source
from gateway.status_inline_source import (
    _bootstrap_entry, _bootstrap_entry_uncached, clear_inline_source_cache, inline_bootstrap_argv)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_inline_source_cache()
    yield
    clear_inline_source_cache()


def _launcher() -> tuple[str, list[str]]:
    """The real payload launcher's ``-c`` source and its trailing argv.

    Rendered by the production builder (``scripts.build.launchers.posix_launcher``) rather
    than hand-written, so this test tracks the shape the parser actually has to recognise.
    """
    from scripts.build.launchers import posix_launcher

    script = posix_launcher(
        "hermes", "hermes_cli.main:main", python="tools/python/bin/python3",
        repo="hermes-agent", site="venv/lib/python3.14/site-packages", target="linux-x64")
    exec_line = next(line for line in script.splitlines() if line.startswith("exec "))
    _exec, _python, *launch, _args = shlex.split(exec_line)
    return launch[launch.index("-c") + 1], ["gateway", "run", "--replace"]


def test_repeated_parse_does_no_pattern_work():
    """The second and hundredth parse of one source must not run the regex ladder again."""
    source, _argv = _launcher()
    first = _bootstrap_entry(source, [])
    assert first is not None, "the production launcher source is not recognised at all"
    # The trailing argv is passed separately, so an empty argv maps to the bare module run.
    assert first == ["-m", "hermes_cli.main"]

    attempts = {"n": 0}
    real = status_inline_source._BOOTSTRAPS

    class CountingPattern:
        def __init__(self, pattern):
            self._pattern = pattern

        def fullmatch(self, text):
            attempts["n"] += 1
            return self._pattern.fullmatch(text)

    status_inline_source._BOOTSTRAPS = tuple(
        (kind, CountingPattern(pattern)) for kind, pattern in real)
    try:
        for _ in range(20):
            assert _bootstrap_entry(source, []) == first
    finally:
        status_inline_source._BOOTSTRAPS = real

    assert attempts["n"] == 0, f"{attempts['n']} regex attempt(s) on a memoised source"


def test_memoised_answer_matches_an_uncached_parse():
    """The cache returns exactly what the parser would have — including the None cases."""
    source, _argv = _launcher()
    assert _bootstrap_entry(source, []) == _bootstrap_entry_uncached(source, [])
    assert _bootstrap_entry("print('hi')", []) is None
    assert _bootstrap_entry(source, ["--verbose"]) == ["-m", "hermes_cli.main", "--verbose"]


def test_argv_is_part_of_the_key():
    """Two different argv values must not share one verdict."""
    source, _argv = _launcher()
    one = _bootstrap_entry(source, [])
    two = _bootstrap_entry(source, ["--log-file", "x"])
    assert one is not None and two is not None
    assert one != two
    assert two[-2:] == ["--log-file", "x"]


def test_inline_argv_is_memoised_per_token_tuple():
    """The outer walk is memoised too: a repeat returns the same list without re-joining."""
    source, argv = _launcher()
    tokens = ["/usr/bin/python3", "-c", source, *argv]
    first = inline_bootstrap_argv(tokens)
    assert first is not None, "the production launcher argv is not recognised at all"
    assert first == ["/usr/bin/python3", "-m", "hermes_cli.main", *argv]

    calls = {"n": 0}
    real = status_inline_source._bootstrap_entry

    def counting(source_text, tail):
        calls["n"] += 1
        return real(source_text, tail)

    status_inline_source._bootstrap_entry = counting
    try:
        for _ in range(20):
            assert inline_bootstrap_argv(tokens) == first
    finally:
        status_inline_source._bootstrap_entry = real

    assert calls["n"] == 0, f"{calls['n']} re-parse(s) of one memoised argv"


def test_mutating_the_returned_list_does_not_corrupt_the_memo():
    """Callers get a copy: a status renderer that sorts or edits its argv must not poison it."""
    source, argv = _launcher()
    tokens = ["/usr/bin/python3", "-c", source, *argv]
    first = inline_bootstrap_argv(tokens)
    assert first is not None
    first.append("MUTATED")

    second = inline_bootstrap_argv(tokens)
    assert second is not None
    assert "MUTATED" not in second, "a caller's edit leaked back into the memo"


def test_cache_is_bounded():
    """A process spawning many short-lived shells must not grow the memo without bound."""
    clear_inline_source_cache()
    for index in range(status_inline_source._BOOTSTRAP_CACHE_MAX + 40):
        _bootstrap_entry(f"print({index})", [])
    assert len(status_inline_source._bootstrap_cache) <= status_inline_source._BOOTSTRAP_CACHE_MAX
