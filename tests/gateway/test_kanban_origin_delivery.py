"""Kanban terminal outbox binding across root/child/review."""
import json
import sqlite3

from gateway.kanban_origin_delivery import stage_terminal
from gateway.origin_delivery_worker import drain_openwebui
from gateway.origin_outbox import status

RAW = "98d69f26-8263-4e1c-84a5-6d9fcdbf593d"
RAW_OTHER = "b5b7fc5b-2222-4222-8333-444455556666"
RAW_THIRD = "c6c8fd6c-3333-4222-8333-555566667777"


def _mk_registry(path, rows):
    """Build a chat registry with {raw_chat_id: owner_user_id}."""
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, chat JSON NOT NULL)")
        for chat_id, owner in rows:
            db.execute("INSERT INTO chat VALUES (?, ?, ?)", (
                chat_id, owner, json.dumps({"history": {
                    "messages": {"u": {"id": "u", "role": "user", "parentId": None}},
                    "currentId": "u",
                }}),
            ))


def test_root_child_review_event_ack_cursor_gate(tmp_path):
    registry = tmp_path / "webui.db"
    outbox = tmp_path / "outbox.db"
    _mk_registry(registry, [(RAW, "owner-A")])
    sub = {"platform": "api_server", "chat_id": "owui-" + RAW, "thread_id": "", "notifier_profile": "01-orchestrator", "user_id": "owner-A"}
    for task_id, kind, event_num in (("root", "completed", 100), ("child", "review_requested", 101), ("child", "completed", 102)):
        msg = f"Kanban {task_id} {kind}"
        state, event = stage_terminal(outbox, registry, sub, "01-orchestrator", "board-A", event_num, msg)
        assert state == "pending"
        assert status(outbox, event) == "pending"
        assert drain_openwebui(outbox, registry)["acknowledged"] == 1
        state2, event2 = stage_terminal(outbox, registry, sub, "01-orchestrator", "board-A", event_num, msg)
        assert (state2, event2) == ("acknowledged", event)
    with sqlite3.connect(registry) as db:
        history = json.loads(db.execute("SELECT chat FROM chat WHERE id=?", (RAW,)).fetchone()[0])["history"]
    assert len(history["messages"]) == 4


def test_untrusted_binding_never_enqueues(tmp_path):
    sub = {"platform": "api_server", "chat_id": "owui-" + RAW, "thread_id": "", "notifier_profile": "01-orchestrator", "user_id": "owner-A"}
    assert stage_terminal(tmp_path / "outbox", tmp_path / "missing.db", sub, "01-orchestrator", "board", 1, "message")[0] == "unbound"
    assert not (tmp_path / "outbox").exists()


def test_non_owner_initiator_gets_itog_in_own_owui_chat(tmp_path, monkeypatch):
    """Итог по задаче НЕ-владельца доставляется инициатору в ЕГО СОБСТВЕННЫЙ
    OWUI-чат (сквозной сценарий A, t_9944a3ba).

    Задача создаётся в durable-create пути для assignee ``default`` (владелец
    задачи), но инициируется ``owner-B`` — не-владельцем — из его личного
    OWUI-чата (session_id=owui-<RAW_OTHER>) с verified ``origin_binding``.  До фикса A4
    (коммит 0aae577c45) этот путь писал ``kanban_notify_subs`` с
    user_id=None → ``stage_terminal`` возвращал ``unbound`` → итог молча не
    уходил.  Тест едет по реальному сеаму записи подписки (не ручной подпиской)
    и обязан:

      - на родителе d3cfc258d5 падать: ``origin_binding`` не существует в
        ``execute_durable_chat_command``;
      - на HEAD проходить: подписка несёт верифицированный user_id=owner-B,
        ``stage_terminal`` резолвит его канал как bound, итог ложится в
        собственный чат owner-B и НЕ попадает в чат владельца задачи
        (assignee).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", str(tmp_path / "webui.db"))
    registry = tmp_path / "webui.db"
    outbox = tmp_path / "outbox.sqlite"
    _mk_registry(registry, [
        (RAW, "owner-A"),
        (RAW_OTHER, "owner-B"),
    ])
    import gateway.durable_chat_tasks as _dt
    original_board = _dt.BOARD
    _dt.BOARD = "nonowner-delivery-board"
    try:
        from gateway.durable_chat_tasks import (
            execute_durable_chat_command, parse_durable_chat_command,
        )
        cmd = parse_durable_chat_command("ДЛИННАЯ ЗАДАЧА\nОтчёт")
        assert cmd is not None and cmd.action == "create"
        result = execute_durable_chat_command(
            cmd,
            session_id="owui-" + RAW_OTHER,   # личный чат инициатора owner-B
            assignee="default",               # владелец задачи (реальный профиль)
            notifier_profile="default",
            origin_binding={
                "chat_id": "owui-" + RAW_OTHER,
                "session_id": "owui-" + RAW_OTHER,
                "user_id": "owner-B",         # верифицированный инициатор (A4)
                "profile": "default",
            },
        )
        assert result.ok, result.message

        from hermes_cli import kanban_db as kb
        conn = sqlite3.connect(kb.kanban_db_path("nonowner-delivery-board"))
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM kanban_notify_subs").fetchone()
            assert row is not None, "durable create должен записать подписку"
            sub = {k: row[k] for k in row.keys()}
        finally:
            conn.close()
        # Не-владелец: подписка несёт верифицированный user_id=owner-B, а НЕ
        # user_id=None (pre-A4) и не user_id владельца задачи owner-A.
        assert sub["user_id"] == "owner-B", (
            f"подписка не-владельца обязана нести verified user_id=owner-B, "
            f"получено {sub['user_id']!r}"
        )
        assert sub["chat_id"] == "owui-" + RAW_OTHER

        message = "✅ Итог не-владельца — в чат инициатора"
        state, delivery_id = stage_terminal(
            outbox, registry, sub, "default", "nonowner-delivery-board",
            500, message,
        )
        assert state in ("pending", "acknowledged"), (
            f"ожидали bound (pending/acknowledged), получили {state}"
        )
        assert delivery_id is not None
        assert drain_openwebui(outbox, registry)["acknowledged"] == 1
        assert status(outbox, delivery_id) == "acknowledged"
        # Итог прочитан обратно из собственного чата инициатора owner-B;
        # в чате владельца задачи owner-A его быть не должно.
        with sqlite3.connect(registry) as db:
            for chat_id, owner in ((RAW, "owner-A"), (RAW_OTHER, "owner-B")):
                history = json.loads(db.execute(
                    "SELECT chat FROM chat WHERE id=?", (chat_id,)).fetchone()[0])["history"]
                msgs = [m["content"] for m in history["messages"].values()
                        if isinstance(m, dict) and m.get("role") == "assistant"]
                if owner == "owner-B":
                    assert message in msgs, (
                        f"итог должен лежать в собственном чате owner-B: {msgs}"
                    )
                else:
                    assert message not in msgs, (
                        f"итог НЕ должен попасть в чат владельца задачи owner-A: {msgs}"
                    )
    finally:
        _dt.BOARD = original_board


def test_only_initiator_isolation_foreign_user_id_fail_closed(tmp_path):
    """«Только инициатору»: подписка с user_id ≠ фактического владельца чата
    НЕ получает итог и НЕ уходит в чужой чат (fail-closed).

    Чат owui-<RAW> принадлежит owner-A.  Подписка навязывает user_id=owner-B
    (посторонний, не владелец чата) — `resolve_origin` обязан отклонить её,
    `stage_terminal` вернуть ``unbound``, в outbox ничего не записано.
    """
    registry = tmp_path / "webui.db"
    outbox = tmp_path / "outbox.db"
    _mk_registry(registry, [
        (RAW, "owner-A"),
        (RAW_OTHER, "owner-B"),
    ])
    sub = {
        "platform": "api_server",
        "chat_id": "owui-" + RAW,       # чат владельца owner-A
        "thread_id": "",
        "notifier_profile": "01-orchestrator",
        "user_id": "owner-B",           # чужой — НЕ владелец chat RAW
    }
    state, delivery_id = stage_terminal(
        outbox, registry, sub, "01-orchestrator", "board-A", 600, "чужой итог",
    )
    assert state == "unbound", f"чужой user_id обязан уйти в unbound, получили {state}"
    assert delivery_id is None
    assert not outbox.exists(), "при unbound не должен создаваться outbox"
    # Чужое сообщение нигде не записано.
    with sqlite3.connect(registry) as db:
        for chat_id in (RAW, RAW_OTHER):
            history = json.loads(db.execute(
                "SELECT chat FROM chat WHERE id=?", (chat_id,)).fetchone()[0])["history"]
            assert all(
                m.get("role") != "assistant" for m in history["messages"].values()
                if isinstance(m, dict)
            ), f"чат {chat_id} не должен содержать чужой итог"


def test_third_user_unrelated_gets_no_final_result_initiator_and_involved_receive(
    tmp_path,
):
    """Изоляция инициатора (t_982599dd): итог получают инициатор и вовлечённый
    участник, а посторонний третий пользователь — НЕТ (ни в свой канал, ни
    куда-либо ещё).

    Сценарий — три РАЗНЫХ пользователя, у каждого свой OWUI-чат в реестре:
      * инициатор owner-A  — чат owui-<RAW>       (создатель задачи),
      * участник owner-B   — чат owui-<RAW_OTHER> (вовлечён в задачу),
      * посторонний owner-C — чат owui-<RAW_THIRD> (не инициатор и не участник).
    Итог стаджится по подписке инициатора и участника — оба получают его в
    СВОИ чаты (положительный контроль).  Посторонний третий не получает НИЧЕГО:
    доставка в его канал возможна только под несовпадающей с владельцем
    identity, которую `resolve_origin` (изоляция «только инициатору», план
    §2.3/§4.3) обязан отклонить → `stage_terminal` ``unbound``, сообщение не
    записывается ни в один чат.
    """
    registry = tmp_path / "webui.db"
    outbox = tmp_path / "outbox.db"
    _mk_registry(registry, [
        (RAW, "owner-A"),        # инициатор
        (RAW_OTHER, "owner-B"),  # вовлечённый участник
        (RAW_THIRD, "owner-C"),  # посторонний третий
    ])
    message = "✅ Итог — только инициатору и участнику"

    # 1) Инициатор получает итог в свой чат (положительный контроль).
    sub_initiator = {
        "platform": "api_server",
        "chat_id": "owui-" + RAW,
        "thread_id": "",
        "notifier_profile": "01-orchestrator",
        "user_id": "owner-A",
    }
    state, delivery_id = stage_terminal(
        outbox, registry, sub_initiator, "01-orchestrator", "board-A", 700, message,
    )
    assert state in ("pending", "acknowledged"), (
        f"инициатор должен быть bound, получили {state}"
    )
    assert delivery_id is not None
    assert drain_openwebui(outbox, registry)["acknowledged"] == 1
    assert status(outbox, delivery_id) == "acknowledged"

    # 2) Вовлечённый участник получает итог в свой чат (положительный контроль).
    sub_involved = {
        "platform": "api_server",
        "chat_id": "owui-" + RAW_OTHER,
        "thread_id": "",
        "notifier_profile": "01-orchestrator",
        "user_id": "owner-B",
    }
    state, delivery_id2 = stage_terminal(
        outbox, registry, sub_involved, "01-orchestrator", "board-A", 701, message,
    )
    assert state in ("pending", "acknowledged"), (
        f"участник должен быть bound, получили {state}"
    )
    assert delivery_id2 is not None
    assert drain_openwebui(outbox, registry)["acknowledged"] == 1
    assert status(outbox, delivery_id2) == "acknowledged"

    # 3) Посторонний третий: его канал НЕ получает итог.  Третий — не инициатор
    #    и не участник; единственный способ, которым чужой канал мог бы
    #    получить итог, — подписка с identity, не совпадающей с фактическим
    #    владельцем канала.  resolve_origin (изоляция «только инициатору»,
    #    план §2.3/§4.3) обязан это отклонить: stage_terminal → unbound,
    #    outbox не трогается, в чат третьего ничего не записывается.
    sub_third_mismatched = {
        "platform": "api_server",
        "chat_id": "owui-" + RAW_THIRD,   # канал ТРЕТЬЕГО (owner-C)
        "thread_id": "",
        "notifier_profile": "01-orchestrator",
        "user_id": "owner-A",             # чужая identity (инициатор) —
                                          # НЕ владелец канала третьего
    }
    state, delivery_id3 = stage_terminal(
        outbox, registry, sub_third_mismatched, "01-orchestrator", "board-A", 702,
        message,
    )
    assert delivery_id3 is None, (
        "посторонний третий не должен получить delivery id"
    )
    assert state == "unbound", (
        "доставка в канал третьего под чужой identity обязана уйти в unbound, "
        f"получили {state}"
    )

    # Итог есть только в чатах инициатора и участника; в чате третьего — нет.
    with sqlite3.connect(registry) as db:
        for chat_id, owner in (
            (RAW, "owner-A"),        # инициатор — должен быть
            (RAW_OTHER, "owner-B"),  # участник — должен быть
            (RAW_THIRD, "owner-C"),  # третий — НЕ должен быть
        ):
            history = json.loads(db.execute(
                "SELECT chat FROM chat WHERE id=?", (chat_id,)).fetchone()[0])["history"]
            msgs = [m["content"] for m in history["messages"].values()
                    if isinstance(m, dict) and m.get("role") == "assistant"]
            if owner in ("owner-A", "owner-B"):
                assert message in msgs, (
                    f"итог должен лежать в собственном чате {owner}: {msgs}"
                )
            else:
                assert message not in msgs, (
                    f"итог НЕ должен попасть в чат постороннего третьего "
                    f"{owner}: {msgs}"
                )
