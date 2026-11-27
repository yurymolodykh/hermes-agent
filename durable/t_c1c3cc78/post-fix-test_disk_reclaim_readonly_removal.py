#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тест удаления read-only остатков уборщиком диска (t_c1c3cc78).

Дефект: `shutil.rmtree(c["path"])` без обработчика падал на первом подкаталоге без
права записи (режимы 000/0300/0555) и оставлял объект навсегда. Каждый прогон
снова писал в журнал «ошибка удаления: [Errno 13] Permission denied» — 1,6 МБ
мусора не убирались никогда (scratch t9be97025-подобные, kanban arc2).

Проверяется поведение, а не текст кода: прогон идёт через настоящий `main()` с
`--manifest --apply`, на временном HERMES_HOME и временной доске. Сеть не нужна,
боевые каталоги и боевая доска не трогаются.

Запуск: /home/hermes/.hermes/hermes-agent/.venv/bin/python -m pytest -q \
        scripts/tests/test_disk_reclaim_readonly_removal.py
"""
import importlib.util
import io
import json
import os
import sqlite3
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve()
SCRIPT = HERE.parent.parent / "disk_reclaim_cron.py"
OLD = time.time() - 30 * 3600  # старше любого порога класса var-tmp


def _load_module(home: Path):
    """Модуль читает HERMES_HOME на импорте, поэтому env ставим до загрузки."""
    old_env = {k: os.environ.get(k) for k in ("HERMES_HOME", "HERMES_REAP_HOME")}
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_REAP_HOME"] = str(home.parent)
    try:
        spec = importlib.util.spec_from_file_location("disk_reclaim_cron_ro_under_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    # tmp_path лежит под .../profiles/<profile>/cache/scratch, поэтому _hermes_home()
    # срезал бы путь до боевого ~/.hermes. Прибиваем дом явно: тест не должен
    # читать боевую доску и не должен писать в боевой журнал.
    mod.HH = home
    mod.HOME = home.parent
    return mod


def _make_board(home: Path, rows=()) -> Path:
    db = home / "kanban.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        create table tasks (id text primary key, status text, body text, workspace_path text);
        create table task_comments (id integer primary key, task_id text, author text,
                                    body text, created_at integer);
        """
    )
    for tid, status, body in rows:
        con.execute("insert into tasks values (?,?,?,null)", (tid, status, body))
        con.execute("insert into task_comments (task_id, author, body, created_at)"
                    " values (?,?,?,?)", (tid, "a", body, int(time.time())))
    con.commit()
    con.close()
    return db


def _age(p: Path) -> None:
    """Состарить дерево, иначе кандидат не пройдёт reverify по возрасту."""
    for node in sorted(p.rglob("*"), key=lambda x: -len(x.parts)):
        os.utime(node, (OLD, OLD))
    os.utime(p, (OLD, OLD))


def _candidate(path: Path) -> dict:
    return {"path": str(path), "class": "var-tmp", "bytes": 1,
            "mtime": round(path.lstat().st_mtime, 3), "why": "тест"}


def _run(mod, home: Path, manifest: list[dict], monkeypatch=None):
    mf = home / "manifest.json"
    mf.write_text(json.dumps({"items": manifest}, ensure_ascii=False), encoding="utf-8")
    argv = ["disk_reclaim_cron.py", "--apply", "--json", "--manifest", str(mf)]
    old_argv = sys.argv
    sys.argv = argv
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = mod.main()
    finally:
        sys.argv = old_argv
    return rc, json.loads(buf.getvalue())


def _readonly_tree(base: Path, mode: int) -> Path:
    """Объект как в журнале: внутри read-only подкаталог с файлом."""
    base.mkdir(parents=True)
    deep = base / "kh" / "boards" / "_archived" / "arc2"
    deep.mkdir(parents=True)
    (deep / "blob.bin").write_bytes(b"x" * 4096)
    os.chmod(deep, mode)
    _age(base)
    return base


def test_readonly_000_subdir_is_removed(tmp_path):
    """Подкаталог режима 000 (как arc2-1795381736/workspaces) не запирает объект."""
    home = tmp_path / "hermes"
    home.mkdir()
    _make_board(home)
    mod = _load_module(home)
    obj = _readonly_tree(tmp_path / "obj000", 0o000)
    try:
        rc, out = _run(mod, home, [_candidate(obj)])
        assert rc == 0
        assert out["refused"] == [], f"отказ не должен остаться: {out['refused']}"
        assert [c["path"] for c in out["candidates"]] == [str(obj)]
        assert out["candidates"][0]["deleted"] is True
        assert not obj.exists(), "объект обязан исчезнуть"
    finally:
        for p in obj.rglob("*"):
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass


def test_readonly_300_subdir_is_removed(tmp_path):
    """Режим 0300 (как t9be97024/tmp/edge-unreadable_indexes/store/indexes)."""
    home = tmp_path / "hermes"
    home.mkdir()
    _make_board(home)
    mod = _load_module(home)
    obj = _readonly_tree(tmp_path / "obj300", 0o300)
    rc, out = _run(mod, home, [_candidate(obj)])
    assert rc == 0
    assert out["refused"] == [], out["refused"]
    assert not obj.exists()


def test_readonly_file_inside_readonly_dir_is_removed(tmp_path):
    """Файл без права записи внутри каталога 0555: chmod нужен и файлу, и родителю."""
    home = tmp_path / "hermes"
    home.mkdir()
    _make_board(home)
    mod = _load_module(home)
    obj = tmp_path / "objfile"
    (obj / "d").mkdir(parents=True)
    (obj / "d" / "f.bin").write_bytes(b"y" * 128)
    os.chmod(obj / "d" / "f.bin", 0o400)
    os.chmod(obj / "d", 0o555)
    _age(obj)
    rc, out = _run(mod, home, [_candidate(obj)])
    assert rc == 0
    assert out["refused"] == [], out["refused"]
    assert not obj.exists()


def test_live_holder_still_protects_the_path(tmp_path):
    """Защита Z3 не сломана: путь из незакрытой карточки не удаляется даже read-only."""
    home = tmp_path / "hermes"
    home.mkdir()
    obj = _readonly_tree(tmp_path / "objheld", 0o000)
    _make_board(home, rows=[("t_open1", "running", f"держу {obj}/kh")])
    mod = _load_module(home)
    try:
        rc, out = _run(mod, home, [_candidate(obj)])
        assert rc == 0
        assert out["candidates"] == [], "защищённый путь не должен попасть в удалённые"
        assert len(out["refused"]) == 1
        assert "t_open1" in out["refused"][0]["refused"]
        assert obj.exists(), "защита из Z3 обязана сохранить объект"
    finally:
        for p in [obj, *obj.rglob("*")]:
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass


def test_second_refusal_is_reported_not_retried_forever(tmp_path):
    """Второй отказ (живая блокировка) пишется в манифест с явной причиной."""
    home = tmp_path / "hermes"
    home.mkdir()
    _make_board(home)
    mod = _load_module(home)
    obj = tmp_path / "objstuck"
    obj.mkdir()
    (obj / "f").write_bytes(b"z")
    _age(obj)

    calls = []
    real = mod.shutil.rmtree

    def fake_rmtree(path, **kw):
        """Имитация живой блокировки: тот же узел отказывает и на повторе."""
        calls.append(path)
        kw["onexc"](os.rmdir, str(obj / "f"), OSError(13, "Permission denied"))

    mod.shutil.rmtree = fake_rmtree
    try:
        rc, out = _run(mod, home, [_candidate(obj)])
    finally:
        mod.shutil.rmtree = real
    assert rc == 0
    assert out["candidates"] == []
    assert len(out["refused"]) == 1
    reason = out["refused"][0]["refused"]
    assert "не удалось даже после снятия прав" in reason, reason
    assert len(calls) == 2, f"ровно одна повторная попытка, было {len(calls)}"


if __name__ == "__main__":
    import tempfile

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as td:
                try:
                    fn(Path(td))
                    print("ok   ", name)
                except AssertionError as ex:
                    failed += 1
                    print("FAIL ", name, "->", ex)
    sys.exit(1 if failed else 0)
