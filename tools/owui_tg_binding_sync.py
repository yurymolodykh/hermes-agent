"""OWUI ⇄ Telegram binding sync (F3, t_0aedc91c, mission tg-delivery-contract).

Консервативный, read-only по источникам синхронизатор серверных привязок
«OWUI-пользователь ⇄ TG-чат» из двух ЛОКАЛЬНЫХ данных:

  * ``webui.db`` (Open WebUI registry; env ``HERMES_OPENWEBUI_DB_PATH``,
    по умолчанию ``<hermes_home>/openwebui/webui.db``) — таблица ``user``:
    ``id`` (UUID), ``name``, ``email``, ``role``.
  * ``channel_directory.json`` (``<hermes_home>/channel_directory.json``) —
    TG-контакты платформы ``telegram`` типа ``dm`` с ``id``/``name``.

Никаких сетевых вызовов, никакой записи в источники.  Результат — JSON-файл
привязок (по умолчанию ``<hermes_home>/tg_bindings.json``), который читает
``hermes_cli/delivery_policy.owui_telegram_users`` как самый низкоприоритетный
источник (не перекрывает env/cfg, fail-closed при повреждении).

ПРАВИЛО СОГЛАСОВАНИЯ (по данным D0, t_3a18c367 §C):
  * Матчится ТОЛЬКО однозначное соответствие: для TG-DM-контакта должно
    существовать РОВНО ОДНО связанное OWUI-имя.
  * Кандидат-ключ — нормализованный латинский токен:
      - полное имя OWUI-пользователя (транслит кириллицы из ``name``):
        точное совпадение ИЛИ покрытие (короткое TG-имя «Мария» ⊂
        «Мария Сафронова»; «Danil» ⊂ «Данил Сербин»);
      - email-local из ``email``: ТОЛЬКО точное равенство (покрывает
        латинские TG-имена вида «Yury Molodykh» == «yurymolodykh»).
  * Короткие имена (< 4 символов), неоднозначность (несколько OWUI-имён
    по одному ключу) и отсутствие совпадения — НЕ матчатся (fail-closed):
    пользователь остаётся OWUI-only.
  * Выходной набор привязок ДОПОЛНИТЕЛЬНО фильтруется по
    ``TELEGRAM_ALLOWED_USERS`` (когда переменная задана): TG-чат, не
    входящий в allowed (+``TELEGRAM_HOME_CHANNEL``), не добавляется —
    зеркало всё равно не доставило бы ему (гейт ``bound_chat in ALLOWED/home``).

Запуск:
  python -m tools.owui_tg_binding_sync            # dry-run: показать кандидатов
  python -m tools.owui_tg_binding_sync --write    # записать файл привязок
  python -m tools.owui_tg_binding_sync --out <path> --write
  python -m tools.owui_tg_binding_sync --db <webui.db> --channel <dir.json>

Код переиспользуем и как библиотека: ``owui_tg_binding_sync.discover_bindings()``.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("owui_tg_binding_sync")

# ---------------------------------------------------------------------------
# Транслит кириллицы → латиница для сравнения имён (не для отображения).
# ---------------------------------------------------------------------------
_CYR2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "iu", "я": "ia",
}
_TRANS = str.maketrans(_CYR2LAT)


def normalize_key(text: str) -> str:
    """Нормализованный ключ для сравнения имён: lowercase, транслит, alnum."""
    low = (text or "").strip().lower()
    translit = low.translate(_TRANS)
    # Схлопнуть дублирующие latin гласные от транслита (zhch, iu...) не нужно:
    # сравниваем полные строки/токены, а не фонетику.
    return re.sub(r"[^a-z0-9]+", "", translit)


def token_keys(text: str) -> List[str]:
    """Отдельные значимые токены (латиница) из имени/email."""
    low = (text or "").strip().lower()
    translit = low.translate(_TRANS)
    toks = re.findall(r"[a-z0-9]{2,}", translit)
    return toks


def split_name_words(text: str) -> List[str]:
    """Слова полного имени (для coverage-проверки коротких TG-имён)."""
    toks = re.findall(r"[а-яa-zё]+", (text or "").lower(), re.IGNORECASE)
    return toks


# ---------------------------------------------------------------------------
# Чтение источников (read-only)
# ---------------------------------------------------------------------------


def default_webui_db_path() -> str:
    env = os.environ.get("HERMES_OPENWEBUI_DB_PATH", "").strip()
    if env:
        return env
    from hermes_constants import get_hermes_home

    return str(get_hermes_home() / "openwebui" / "webui.db")


def default_channel_dir_path() -> str:
    from hermes_constants import get_hermes_home

    return str(get_hermes_home() / "channel_directory.json")


def load_owui_users(db_path: str) -> List[Dict[str, str]]:
    """Прочитать OWUI-пользователей (read-only, ``mode=ro``)."""
    if not os.path.exists(db_path):
        logger.warning("webui.db not found at %s", db_path)
        return []
    try:
        con = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=5.0
        )
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(user)")]
            wanted = [c for c in ("id", "name", "email", "role") if c in cols]
            if "id" not in wanted or "name" not in wanted:
                logger.error("webui.db user table lacks id/name — abort")
                return []
            rows = con.execute(
                "SELECT %s FROM user" % ", ".join(wanted)
            ).fetchall()
            out = []
            for row in rows:
                rec = dict(zip(wanted, row))
                out.append({
                    "id": str(rec.get("id") or "").strip(),
                    "name": str(rec.get("name") or "").strip(),
                    "email": str(rec.get("email") or "").strip(),
                    "role": str(rec.get("role") or "").strip(),
                })
            return [r for r in out if r["id"]]
        finally:
            con.close()
    except Exception as exc:
        logger.error("reading webui.db %s failed: %s", db_path, exc)
        return []


def load_tg_dm_contacts(channel_path: str) -> List[Dict[str, str]]:
    """TG-DM-контакты из channel_directory.json (read-only)."""
    if not os.path.exists(channel_path):
        logger.warning("channel_directory.json not found at %s", channel_path)
        return []
    try:
        with open(channel_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.error("reading channel_directory %s failed: %s", channel_path, exc)
        return []
    entries = (
        (data or {}).get("platforms", {}).get("telegram", [])
        if isinstance(data, dict) else []
    )
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        ctype = str(e.get("type") or e.get("chat_type") or "dm").lower()
        if ctype not in ("dm", "direct", "private"):
            continue
        cid = str(e.get("id") or "").strip()
        if not cid:
            continue
        out.append({
            "chat_id": cid,
            "name": str(e.get("name") or "").strip(),
            "type": ctype,
        })
    return out


def _user_match_keys(user: Dict[str, str]) -> List[str]:
    """Все нормализованные ключи, по которым OWUI-пользователь «опознаётся».

    Полный normalized name (name), normalized email-local (до @), имя+фамилия
    токенами (для coverage).  Собираем в set, чтобы дедуплицировать.
    """
    keys = set()
    name = user.get("name") or ""
    email = user.get("email") or ""
    if name:
        keys.add(normalize_key(name))
        for w in split_name_words(name):
            if len(w) >= 3:
                keys.add(normalize_key(w))
    if email:
        local = email.split("@", 1)[0]
        keys.add(normalize_key(local))
        for w in re.findall(r"[a-z0-9]+", local.lower()):
            if len(w) >= 3:
                keys.add(w)
    return sorted(k for k in keys if k)


def _contact_candidates(
    contact: Dict[str, str], users: List[Dict[str, str]]
) -> List[Dict[str, str]]:
    """OWUI-пользователи, которые однозначно соответствуют TG-контакту.

    Правило согласования (D0 §C4, консервативное, fail-closed):

      * Нормализованное имя контакта должно иметь >= 4 значимых символа
        (короткие/голые «M M», числовые id и т.п. не матчатся).
      * Кандидат — ровно ОДИН OWUI-пользователь, у которого нормализованное
        имя (транслит) РАВНО имени контакта ИЛИ содержится в нём ИЛИ
        содержит его (покрытие, включая сокращённые TG-имена вида «Мария»,
        «Danil» при полном имени «Мария Сафронова»/«Данил Сербин»).
      * Неоднозначность (несколько пользователей с одним нормализованным
        именем) → пусто (без матча, OWUI-only).
      * Отсутствие покрытия → пусто.

    Возвращает список из 0 или 1 пользователя (список для единообразной
    проверки ``len(cands) == 1`` на месте вызова).
    """
    cname = normalize_key(contact.get("name") or "")
    if not cname or len(cname) < 4:
        return []
    hits = []
    for u in users:
        uname = normalize_key(u.get("name") or "")
        email = u.get("email") or ""
        ulocal = normalize_key(email.split("@", 1)[0]) if email else ""
        # Полное имя (транслит кириллицы) — основной ключ: точное совпадение
        # ИЛИ покрытие (короткое TG-имя «Мария» ⊂ «Мария Сафронова»;
        # «Danil» ⊂ «Данил Сербин»).  Это правило воспроизводит
        # D0 §C4-подтверждённые пары (Мария, Ксения, Владимир, Данил,
        # Сергей …) и НЕ находит неподтверждённых.
        if uname and len(uname) >= 4 and (
            cname == uname or cname in uname or uname in cname
        ):
            hits.append(u)
            continue
        # Email-local — ТОЛЬКО точное равенство (не substring): «Yury
        # Molodykh» == «yurymolodykh» точно, но короткое «Dmitry» НЕ
        # матчится на email «dmitry.borodulin» (D0 не подтверждает эту
        # пару — fail-closed).
        if ulocal and len(ulocal) >= 4 and cname == ulocal:
            hits.append(u)
    if len(hits) == 1:
        return hits
    # Если полное имя не дало результата (напр. TG «Abramova Anastasia»
    # против «Анастасия Абрамова») — не гадаем; D0 фиксирует только
    # однозначные пары, остальные остаются OWUI-only.
    return []


# ---------------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------------


def allowed_chat_filter(add: Dict[str, str]) -> Dict[str, str]:
    """Оставить только те привязки, чей TG-чат в ALLOWED/home (когда задан)."""
    raw_allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "").strip()
    home = os.environ.get("TELEGRAM_HOME_CHANNEL", "").strip()
    if not raw_allowed and not home:
        return add
    allowed = {
        str(x).strip()
        for x in raw_allowed.split(",")
        if str(x).strip()
    }
    if home:
        allowed.add(home)
    if not allowed:
        return {}
    return {
        uid: chat for uid, chat in add.items() if str(chat).strip() in allowed
    }


def discover_bindings(
    db_path: Optional[str] = None,
    channel_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Собрать однозначные привязки OWUI→TG из локальных источников.

    Возвращает dict: ``{"bindings": {owui_user_id: tg_chat_id},
    "matches": [...], "skipped": [...]}``.  ``matches``/``skipped`` —
    человекочитаемые записи для dry-run-отчёта.
    """
    db_path = db_path or default_webui_db_path()
    channel_path = channel_path or default_channel_dir_path()
    users = load_owui_users(db_path)
    contacts = load_tg_dm_contacts(channel_path)

    bindings: Dict[str, str] = {}
    matches: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    for contact in contacts:
        cands = _contact_candidates(contact, users)
        if not cands:
            skipped.append({
                "chat_id": contact["chat_id"],
                "contact_name": contact.get("name") or "",
                "reason": "no unambiguous OWUI match (fail-closed)",
            })
            continue
        user = cands[0]
        bindings[user["id"]] = contact["chat_id"]
        matches.append({
            "chat_id": contact["chat_id"],
            "contact_name": contact.get("name") or "",
            "owui_user_id": user["id"],
            "owui_user_name": user.get("name") or "",
            "role": user.get("role") or "",
        })

    # ALLOWED-фильтр (правило D0 §C / гейт зеркала).
    pre = dict(bindings)
    bindings = allowed_chat_filter(bindings)
    for uid, chat in pre.items():
        if chat not in bindings.values() or bindings.get(uid) != chat:
            user_name = next(
                (m["owui_user_name"] for m in matches if m["owui_user_id"] == uid),
                uid,
            )
            skipped.append({
                "chat_id": chat,
                "owui_user_id": uid,
                "owui_user_name": user_name,
                "reason": "TG chat not in TELEGRAM_ALLOWED_USERS/home (mirror gate)",
            })
            matches = [m for m in matches if m["owui_user_id"] != uid]

    return {
        "bindings": bindings,
        "matches": matches,
        "skipped": skipped,
        "sources": {
            "webui_db": db_path,
            "channel_directory": channel_path,
            "allowlist_filtered": bool(
                os.environ.get("TELEGRAM_ALLOWED_USERS", "")
                or os.environ.get("TELEGRAM_HOME_CHANNEL", "")
            ),
        },
    }


def write_bindings_file(
    bindings: Dict[str, str],
    out_path: str,
    *,
    sources: Optional[Dict[str, Any]] = None,
) -> None:
    """Записать файл привязок (атомарно через временный файл + rename)."""
    from datetime import datetime, timezone

    payload = {
        "bindings": {str(k): str(v) for k, v in bindings.items()},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources or {},
        "schema": "tg-delivery-contract/F3-bindings-v1",
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, out_path)
    logger.info("wrote %d bindings to %s", len(bindings), out_path)


def default_out_path() -> str:
    from hermes_constants import get_hermes_home

    return str(get_hermes_home() / "tg_bindings.json")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="owui_tg_binding_sync",
        description=(
            "Консервативный синк привязок OWUI⇄TG из webui.db + "
            "channel_directory.json в tg_bindings.json."
        ),
    )
    p.add_argument("--db", help="путь к webui.db (по умолчанию из env/каталога)")
    p.add_argument(
        "--channel", help="путь к channel_directory.json (по умолчанию каталог)"
    )
    p.add_argument(
        "--out",
        help=(
            "выходной JSON-файл привязок (по умолчанию "
            "<hermes_home>/tg_bindings.json)"
        ),
    )
    p.add_argument(
        "--write",
        action="store_true",
        help="записать файл привязок (без флага — только dry-run)",
    )
    p.add_argument("--verbose", action="store_true", help="лог на stderr")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    result = discover_bindings(args.db, args.channel)
    bindings = result["bindings"]
    matches = result["matches"]
    skipped = result["skipped"]

    print("=== OWUI⇄TG однозначные привязки ===")
    for m in matches:
        print(
            f"  MATCH {m['chat_id']} ({m['contact_name']}) -> "
            f"{m['owui_user_id']} ({m['owui_user_name']}, {m.get('role','')})"
        )
    print(f"  итого матчей: {len(matches)}")
    print()
    print("=== Пропущено (fail-closed) ===")
    for s in skipped:
        print(
            f"  - {s.get('chat_id','?')} ({s.get('contact_name','?')}): "
            f"{s.get('reason')}"
        )
    print(f"  итого пропущено: {len(skipped)}")
    print()

    if args.write:
        out = args.out or default_out_path()
        write_bindings_file(bindings, out, sources=result["sources"])
        print(f"Файл привязок записан: {out}")
        print(f"  bindings: {len(bindings)}")
        return 0
    print(
        "Dry-run: ничего не записано. Повторите с --write для записи "
        "файла привязок."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
