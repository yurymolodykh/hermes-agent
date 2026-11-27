#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тест защиты Z3: комментарии ЗАКРЫТЫХ карточек не держат пути (t_d80665aa).

Сценарий дефекта: `/var/tmp/cgrev` (541 МиБ мусора) упоминался только в комментариях
карточек со статусами done/done/done/archived. Запрос без join по статусу защищал его
навсегда — защита превращалась в утечку.

Сеть не нужна; боевая доска не читается — только временная kanban.db в tmp.
Запуск: /usr/bin/python3 -m pytest -q scripts/tests/test_disk_reclaim_card_refs.py
"""
import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "disk_reclaim_cron.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("disk_reclaim_cron_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_board(tmp_path: Path, p: dict) -> Path:
    """Минимальная копия схемы доски: только нужные тесту колонки.

    Пути в телах/комментариях — реальные каталоги во временном дереве теста,
    поэтому `check()` проверяет их по-настоящему (существование не требуется,
    но подмена пути не должна маскировать правило `under`).
    """
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        create table tasks (id text primary key, status text, body text, workspace_path text);
        create table task_comments (id integer primary key, task_id text, author text,
                                    body text, created_at integer);
        """
    )
    now = int(time.time())
    rows = [
        # (id, status, body, workspace_path)
        ("t_closed1", "done", "см. %s/h26.py" % p["cgrev"], "%s/closed1" % tmp_path),
        ("t_closed2", "archived", "и ещё %s" % p["cgrev"], "%s/closed2" % tmp_path),
        ("t_open1", "running", None, p["open_ws"]),
        ("t_open2", "ready", "тело открытой: %s" % p["open_body"], None),
    ]
    con.executemany("insert into tasks values (?,?,?,?)", rows)
    con.executemany(
        "insert into task_comments (task_id, author, body, created_at) values (?,?,?,?)",
        [
            ("t_closed1", "a", "проверял %s — мусор" % p["cgrev"], now),
            ("t_closed2", "b", "`%s/r1/hard.dat` тоже там" % p["cgrev"], now),
            ("t_open1", "c", "живой держатель: %s" % p["open_comment"], now),
        ],
    )
    con.commit()
    con.close()
    return db


def setup_tmp(tmp_path: Path) -> dict:
    """Объекты во временном дереве: имитируют /var/tmp/cgrev и каталоги-держатели."""
    base = tmp_path / "var-tmp"
    base.mkdir(exist_ok=True)
    for name in ("cgrev", "open_comment_obj", "open_body_obj"):
        d = base / name
        d.mkdir()
        (d / "f.bin").write_bytes(b"x" * 64)
    (base / "cgrev" / "r1").mkdir(exist_ok=True)
    return {
        "cgrev": str(base / "cgrev"),
        "open_comment": str(base / "open_comment_obj"),
        "open_body": str(base / "open_body_obj"),
        "closed_ws": str(tmp_path / "ws" / "closed1"),
        "open_ws": str(tmp_path / "ws" / "open1"),
    }


def card_of(cards, path: str, mod=None) -> str | None:
    """Какой карточкой защищён путь (по правилу under из скрипта).

    Принимает и dict {путь: id} (после правки), и set (старая ревизия): для set
    возвращается сам путь — тогда красное падение будет по существу (путь защищён),
    а не по форме возврата функции.
    """
    under = mod.under
    items = cards.items() if hasattr(cards, "items") else ((c, c) for c in cards)
    for cp, cid in items:
        if under(cp, path):
            return cid
    return None


def test_closed_comment_does_not_protect(tmp_path):
    mod = _load_module()
    p = setup_tmp(tmp_path)
    db = make_board(tmp_path, p)
    cards = mod.open_card_paths(db)
    assert card_of(cards, p["cgrev"], mod) is None, (
        "комментарий закрытой карточки не должен защищать путь: %r" % (cards,))
    why = mod.check({"path": p["cgrev"], "class": "var-tmp"}, set(), cards)
    assert why is None, "push-кандидат должен быть чистым, получено: %r" % (why,)


def test_open_comment_protects(tmp_path):
    mod = _load_module()
    p = setup_tmp(tmp_path)
    db = make_board(tmp_path, p)
    cards = mod.open_card_paths(db)
    why = mod.check({"path": p["open_comment"], "class": "var-tmp"}, set(), cards)
    assert why is not None and why.startswith("ссылка из незакрытой карточки t_open1:"), why


def test_open_workspace_path_protects(tmp_path):
    mod = _load_module()
    p = setup_tmp(tmp_path)
    db = make_board(tmp_path, p)
    cards = mod.open_card_paths(db)
    assert card_of(cards, p["open_ws"], mod) == "workspace:t_open1"
    why = mod.check({"path": p["open_ws"], "class": "scratch"}, set(), cards)
    assert why is not None and "t_open1" in why, why


def test_closed_workspace_path_not_protected(tmp_path):
    mod = _load_module()
    p = setup_tmp(tmp_path)
    db = make_board(tmp_path, p)
    cards = mod.open_card_paths(db)
    assert card_of(cards, p["closed_ws"], mod) is None


def test_open_body_protects_and_reason_names_card(tmp_path):
    mod = _load_module()
    p = setup_tmp(tmp_path)
    db = make_board(tmp_path, p)
    cards = mod.open_card_paths(db)
    why = mod.check({"path": p["open_body"], "class": "var-tmp"}, set(), cards)
    assert why == f"ссылка из незакрытой карточки t_open2: {p['open_body']}", why


def test_live_process_still_protects(tmp_path):
    """Z2 не сломан правкой: живой процесс держит путь."""
    mod = _load_module()
    p = setup_tmp(tmp_path)
    db = make_board(tmp_path, p)
    cards = mod.open_card_paths(db)
    why = mod.check({"path": p["cgrev"], "class": "var-tmp"},
                    {p["cgrev"] + "/f.bin"}, cards)
    assert why is not None and why.startswith("живой процесс держит"), why


def test_ignore_card_ref_drops_mention_keeps_workspace(tmp_path):
    """Операторское исключение снимает только упоминания в тексте, но не workspace_path.

    Иначе карточка, которая сама цитирует удаляемый объект (как t_d80665aa — `/var/tmp/cgrev`),
    запирала бы его навсегда, а вместе с ним терялась бы и настоящая защита её рабочей директории.
    """
    mod = _load_module()
    p = setup_tmp(tmp_path)
    db = make_board(tmp_path, p)
    cards = mod.open_card_paths(db)
    assert cards[p["open_ws"]] == "workspace:t_open1"
    kept = {k: v for k, v in cards.items()
            if v.startswith("workspace:") or v != "t_open1"}
    # упоминание в комментарии t_open1 снято …
    assert mod.check({"path": p["open_comment"], "class": "var-tmp"}, set(), kept) is None
    # … а рабочая директория t_open1 по-прежнему защищена
    why = mod.check({"path": p["open_ws"], "class": "scratch"}, set(), kept)
    assert why is not None and "t_open1" in why, why


def test_live_unix_socket_protects_its_path():
    """Живой unix-сокет должен защищать свой путь.

    В /proc/<pid>/fd сокет виден как `socket:[inode]` без пути, поэтому Z2 его не
    ловил, и живой /tmp/hermes_rpc_*.sock удалялся (наблюдено 02:50 24.11.2026).
    Путь берётся из /proc/net/unix по inode.
    """
    mod = _load_module()
    live = mod.live_paths()
    socks = sorted(p for p in live if p.endswith(".sock"))
    assert socks, "live_paths не нашёл ни одного пути сокета — датчик сломан"
    for s in socks[:5]:
        why = mod.check({"path": s, "class": "tmp"}, live, {})
        assert why is not None, f"живой сокет {s} не защищён"


if __name__ == "__main__":
    import tempfile
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as td:
                try:
                    fn(Path(td))
                    print(f"PASS {name}")
                except AssertionError as ex:
                    failed += 1
                    print(f"FAIL {name}: {ex}")
    sys.exit(1 if failed else 0)
