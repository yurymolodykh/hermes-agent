"""Focused unit tests for ``_strip_existing_local_paths`` in api_server.py.

This module is deliberately self-contained (imports only the helper from
``gateway.platforms.api_server``).  It was written while
``tests/gateway/test_api_server.py`` could not be collected because of a
missing ``_classify_openwebui_service_request`` symbol (a separate
OWUI-origin-binding feature); card t_635176e3 removed those tests.

Contract under test (task t_3e687b88):
  * only absolute local filesystem paths that EXIST on this machine are
    removed from the input text;
  * a path that does not exist is left untouched;
  * paths inside backticks (``...``) or fenced code blocks (`````...`````)
    are left untouched;
  * all other text is unchanged.
"""

import tempfile
from pathlib import Path

import pytest

from gateway.platforms.api_server import _strip_existing_local_paths


@pytest.fixture
def existing_file(tmp_path):
    """A real on-disk absolute-path file with a deliverable extension."""
    f = tmp_path / "anonymized.docx"
    f.write_bytes(b"PK\x03\x04test-docx")
    return f


def _as_text(win, posix):
    """Pick the path spelling that the running OS will treat as absolute."""
    return win if win.startswith("C:") or not posix else posix


def test_removes_existing_absolute_path(existing_file):
    text = f"Готовый файл: {existing_file}\nследующая строка"
    out = _strip_existing_local_paths(text)
    assert str(existing_file) not in out
    assert "Готовый файл:" in out
    assert "следующая строка" in out


def test_keeps_non_existing_path(existing_file, tmp_path):
    missing = tmp_path / "missing.docx"
    text = f"Не найден: {missing}"
    out = _strip_existing_local_paths(text)
    assert str(missing) in out


def test_keeps_path_inside_inline_backticks(existing_file):
    text = f"Пример: `{existing_file}`"
    out = _strip_existing_local_paths(text)
    assert str(existing_file) in out


def test_keeps_path_inside_fenced_code_block(existing_file):
    text = f"```\n{existing_file}\n```"
    out = _strip_existing_local_paths(text)
    assert str(existing_file) in out


def test_mixed_content_only_existing_removed(existing_file, tmp_path):
    missing = tmp_path / "missing.docx"
    example = existing_file.parent / "example.txt"
    example.write_bytes(b"plain")
    text = (
        f"Готовый файл: {existing_file}\n"
        f"Не найден: {missing}\n"
        f"Пример: `{example}`"
    )
    out = _strip_existing_local_paths(text)
    assert str(existing_file) not in out
    assert str(missing) in out
    assert str(example) in out  # inside backticks


def test_empty_text_unchanged():
    assert _strip_existing_local_paths("") == ""
    assert _strip_existing_local_paths(None) is None


def test_text_without_paths_unchanged():
    text = "Просто текст без вложений."
    assert _strip_existing_local_paths(text) == text


def test_windows_drive_path_variant(existing_file, tmp_path):
    """Windows drive-letter spelling (C:/... and C:\\...) is handled."""
    if not str(existing_file).startswith(("C:", "D:", "E:")):
        pytest.skip("test runs on non-Windows; drive-letter spelling not applicable")
    win_fwd = str(existing_file).replace("\\", "/")
    text = f"Путь: {win_fwd}"
    out = _strip_existing_local_paths(text)
    assert win_fwd not in out
