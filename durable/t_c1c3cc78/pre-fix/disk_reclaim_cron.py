#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""disk_reclaim_cron.py — ночная дочистка диска без участия LLM (no_agent).

ЗАЧЕМ. Штатный disk_cleanup.py ходит только по /tmp, ~/.hermes/cache и ~/.hermes/logs
и НЕ видит главных пожирателей: scratch прогонов карточек, rollback-поколения RAG,
каталоги backups-managed, /var/tmp. Плюс free_stop (свободно < 20 ГБ → отмена)
отключает уборку ровно тогда, когда места мало.

ЧТО ДЕЛАЕТ. Удаляет только то, что доказано безопасно, по классам:
  A. <profile>/cache/scratch/*        — прогонные копии карточек;
  B. ~/.hermes/rag-deployments/incremental-*/  — rollback-поколения RAG;
  C. ~/.hermes/backups-managed/*      — управляемые бэкапы с истёкшим TTL;
  D. /tmp/* и /var/tmp/*              — временные объекты старше 6 ч;
  E. ~/.hermes/skills/.curator_backups/<старое> — только не самое свежее поколение;
  F. ~/.hermes/cache/*                — старше 7 дней, кроме cache/scratch;
  G. ~/.cache/{pip,uv,pnpm,node-gyp,huggingface} — старше 7 дней, с предпочтением
     `uv cache prune` / `pnpm store prune` перед rm; camoufox и ms-playwright не трогаем;
  H. ~/.hermes/logs/*.log и profiles/*/logs/*.log — обрезка до 2000 строк (copytruncate).

ЗАЩИТЫ (несовпадение любой = кандидат остаётся, причина в журнал):
  Z1 возраст   : ДВУСТОРОННЕЕ окно mtime — объект старше N часов И его mtime не в
                 будущем. Иначе будущий mtime (`/tmp/pytest-of-hermes/pytest-1630/
                 config.yaml` = 2027-01-15) навсегда запирал 710 МиБ при «успехе» в журнале;
  Z2 живой проц: ни один процесс не имеет cwd/exe/открытого файла внутри (читается /proc;
                 недоступный /proc трактуется как «живой», т.е. запрет);
  Z3 карточка  : путь не упомянут у НЕЗАКРЫТОЙ карточки доски (workspace_path/body/комментарии);
                 комментарии закрытых карточек (done/archived) не защищают ничего — иначе
                 мусор, упомянутый только в комментарии закрытой карточки, не убирался бы
                 никогда (t_d80665aa: /var/tmp/cgrev, 541 МиБ);
  Z4 стоп-лист : путь не под исключениями, не симлинк, не чужой uid (sticky /tmp),
                 нет свежих файлов внутри (двусторонне), live-поколение RAG не трогается.

РЕЖИМЫ:
  dry-run по умолчанию (печатает, что бы удалил);
  --apply — боевой;
  --json — машинный вывод;
  --manifest-out PATH — записать сухой манифест (JSON: path, class, bytes, mtime, why);
  --apply --manifest PATH — удалить СТРОГО перечисленное в манифесте; каждый путь
  перепроверяется (существование, mtime, Z2, Z3, стоп-лист), расхождение → skip + причина;
  --ignore-card-ref ID[,ID] — операторское исключение: упоминания этой карточки в тексте
  не защищают пути (её workspace_path защищает по-прежнему). Нужно, когда карточка сама
  цитирует удаляемый объект и иначе запирает его навсегда; факт исключения пишется
  в журнал строкой kind=ignore_card_ref.

ЖУРНАЛ ~/.hermes/logs/disk-prune.log: строка на прогон, строка на класс (объектов/МБ),
итоговый summary JSON. Дублируется построчный журнал отказов в disk-reclaim.log
(исторический формат, совместимость).

Кроном вызывается с --apply --json; пустой досье не печатается (крон молчит).

КОДЫ: 0 — прогон выполнен; 2 — нет доступа к доске/правилам/манифесту (уборка не выполнялась).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HERMES_REAP_HOME", "/home/hermes"))


def _hermes_home() -> Path:
    """HERMES_HOME может указывать и на ~/.hermes, и на профиль (в воркере карточки
    это <hh>/profiles/<name>). Берём корень Hermes в обоих случаях."""
    raw = os.environ.get("HERMES_HOME") or str(HOME / ".hermes")
    p = Path(raw)
    try:
        parts = p.resolve().parts
    except OSError:
        parts = p.parts
    if "profiles" in parts:
        i = len(parts) - 1 - parts[::-1].index("profiles")
        if i >= 1:
            return Path(*parts[:i])
    return p


HH = _hermes_home()
HOUR = 3600.0
DAY = 86400.0
GIB = 1024.0 ** 3
MIB = 1024.0 ** 2

# Стоп-лист: живые данные, которые не трогаем никогда.
STOP_NAMES = frozenset({
    "state.db", "kanban.db", "memory.db", "config.yaml", "SOUL.md",
    "nextcloud-sync", "win-transfer", "hermes-upgreat", "knowledge_base",
    "durable", "attachments", "sessions", "skills", "backups-managed",
})
# Подкаталоги профиля, которые нельзя чистить даже внутри cache/scratch.
SCRATCH_STOP = frozenset({"base_tree", "repos", "live",
                          "node-compile-cache", "pip", "uv", "npm"})
# Документы и таблицы = пользовательские материалы. В скретче им НЕЛЬЗЯ уезжать по
# общему сроку: прогон 22:52 удалил ug_brand.pdf, tr_koz2.pdf, kz.doc, kz.docx, kz.txt
# «как прогонные копии». Для них отдельный, длинный срок (scratch_docs_min_age_days).
SCRATCH_DOC_SUFFIXES = frozenset({
    ".pdf", ".doc", ".docx", ".docm", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx",
    ".pptm", ".odt", ".ods", ".odp", ".rtf",
})
# Стоп-имена в /tmp и /var/tmp: системные и под чужим владением.
TMP_STOP_NAMES = frozenset({
    "pytest-current", "pytest-of-hermes", "lost+found", ".X11-unix", ".ICE-unix",
    ".XIM-unix", ".font-unix", ".Test-unix", "snap-private-tmp", "pulse-hermes",
    "systemd-private", "tmp-exec",
})
TMP_STOP_PREFIXES = ("_apt", "systemd-", "snap-private-tmp", "pytest-of-hermes",
                     "ssh-", "tmp-exec-", "pulse-", "dbus-", "tmux-")
# ~/.hermes/cache: что не трогаем никогда (scratch — отдельный класс, images — свежие).
HERMES_CACHE_STOP = frozenset({"scratch", "images", "screenshots", "videos"})
# ~/.cache: чистим только эти (по указанию карточки); camoufox/ms-playwright — запрет.
USER_CACHE_ALLOW = ("pip", "uv", "pnpm", "node-gyp", "huggingface")
USER_CACHE_NEVER = frozenset({"camoufox", "ms-playwright", "chromium",
                              "google-chrome-for-testing"})

DEFAULT_RULES = {
    "scratch_min_age_hours": 6,
    "scratch_inner_fresh_hours": 2,
    "scratch_docs_min_age_days": 7,
    "tmp_min_age_hours": 6,
    "tmp_inner_fresh_hours": 6,
    "var_tmp_min_age_hours": 6,
    "rag_rollback_min_age_days": 7,
    "keep_rag_rollbacks": 1,
    "keep_curator_generations": 1,
    "hermes_cache_min_age_days": 7,
    "user_cache_min_age_days": 7,
    "log_max_lines": 2000,
    "max_bytes_per_run": 80 * 1024 ** 3,
    "mtime_tolerance_s": 120,
}


def log_path() -> Path:
    d = HH / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "disk-prune.log"


def legacy_log_path() -> Path:
    d = HH / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "disk-reclaim.log"


def journal(rows: list[dict]) -> None:
    """Построчный журнал отказов/удалений (исторический формат disk-reclaim.log)."""
    if not rows:
        return
    with legacy_log_path().open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def journal_run(record: dict) -> None:
    """Структурный журнал прогона: строка на прогон / класс / summary."""
    with log_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- датчики
def live_paths() -> set[str]:
    """cwd, exe и открытые файлы всех доступных процессов. Недоступное = запрет."""
    out: set[str] = set()
    proc = Path("/proc")
    if not proc.is_dir():
        raise RuntimeError("/proc недоступен")
    unreadable = 0
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        for link in ("cwd", "exe"):
            try:
                out.add(os.readlink(pid_dir / link))
            except OSError:
                pass
        try:
            for fd in (pid_dir / "fd").iterdir():
                try:
                    out.add(os.readlink(fd))
                except OSError:
                    pass
        except OSError:
            unreadable += 1
    # Если почти весь /proc нечитаем — датчик бесполезен, лучше отказаться от уборки.
    if unreadable > 0 and len(out) < 50:
        raise RuntimeError(f"/proc почти нечитаем (unreadable={unreadable})")
    # Живые unix-сокеты: в /proc/<pid>/fd они видны как `socket:[inode]`, без пути,
    # поэтому Z2 их не ловит — и прогон 02:50 удалил живой /tmp/hermes_rpc_*.sock.
    # Путь живого сокета берём из /proc/net/unix по inode.
    live_inodes = set()
    for fd_link in out:
        m = re.fullmatch(r"socket:\[(\d+)\]", fd_link)
        if m:
            live_inodes.add(m.group(1))
    for table in ("/proc/net/unix",):
        try:
            with open(table) as fh:
                next(fh, None)
                for line in fh:
                    parts = line.split()
                    # поля: Num RefCount Protocol Flags Type St Inode [Path]
                    if len(parts) >= 8 and parts[6] in live_inodes:
                        out.add(parts[7])
        except OSError:
            pass
    return {p for p in out if p.startswith("/")}


def board_path() -> Path | None:
    for cand in (HH / "kanban.db", HH.parent / "kanban.db", Path("/home/hermes/.hermes/kanban.db")):
        try:
            if cand.is_file() and cand.stat().st_size > 4096:
                return cand
        except OSError:
            continue
    return None


def open_card_paths(db: Path) -> dict[str, str]:
    """Пути, защищённые ОТКРЫТЫМИ карточками: {путь: id карточки-держателя}.

    Комментарии защищают только у открытых карточек (join task_comments→tasks).
    Без join комментарий закрытой карточки держал бы объект навсегда: `/var/tmp/cgrev`
    (541 МиБ) оставался мусором, потому что упоминался в комментариях трёх done-карточек
    и одной archived, а в журнале печаталось «ссылка из незакрытой карточки» (t_d80665aa).

    Ключ карточки-«workspace:» отличает рабочую директорию от упоминания в тексте:
    операторский --ignore-card-ref снимает только упоминания, но не workspace.
    """
    paths: dict[str, str] = {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for tid, wp in con.execute(
            "select id, workspace_path from tasks where status not in ('done','archived')"
            " and workspace_path is not null"
        ):
            paths.setdefault(str(wp).rstrip("/"), "workspace:" + str(tid))
        q = ("select c.task_id, c.body from task_comments c join tasks t on t.id = c.task_id"
             " where t.status not in ('done','archived') and c.body is not null"
             " union all"
             " select id, body from tasks where status not in ('done','archived')"
             " and body is not null")
        for tid, body in con.execute(q):
            for tok in str(body).replace("\n", " ").split():
                tok = tok.strip("`'\"()[]<>,;:")
                if tok.startswith("/home/") or tok.startswith("/tmp/") or tok.startswith("/var/"):
                    paths.setdefault(tok.rstrip("/"), str(tid))
    finally:
        con.close()
    return paths


def under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def py_size(p: Path) -> int:
    if p.is_symlink():
        return 0
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(p, topdown=True):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def tree_newest(p: Path) -> float:
    """Максимальный mtime в дереве (сам объект + содержимое). 0 — не читается."""
    try:
        newest = p.lstat().st_mtime if not p.is_symlink() else 0.0
    except OSError:
        return 0.0
    if p.is_symlink() or not p.is_dir():
        return newest
    for root, dirs, files in os.walk(p, topdown=True):
        for name in files + dirs:
            fp = os.path.join(root, name)
            try:
                mt = os.lstat(fp).st_mtime
            except OSError:
                continue
            if mt > newest:
                newest = mt
    return newest


def old_enough(mtime: float, now: float, min_age_hours: float) -> bool:
    """ДВУСТОРОННЕЕ окно: старше порога И mtime не в будущем."""
    return mtime <= now - min_age_hours * HOUR and mtime <= now


def is_doc_entry(name: str) -> bool:
    """Документ или таблица по расширению — пользовательский материал."""
    return Path(name).suffix.lower() in SCRATCH_DOC_SUFFIXES


def dir_has_docs(p: Path) -> bool:
    """Документ/таблица лежит НЕПОСРЕДСТВЕННО в каталоге (не в глубине)?

    Только верхний уровень: рекурсивный обход помечал «документными» снимки
    рабочих деревьев на 3 ГиБ, у которых в глубине лежит шаблон
    colm2025_conference.pdf, и запирал их на 7 суток вместо 6 часов.
    """
    if not p.is_dir():
        return False
    try:
        entries = list(p.iterdir())
    except OSError:
        return False
    return any(e.is_file() and not e.is_symlink() and is_doc_entry(e.name) for e in entries)


def scratch_age_hours(p: Path, rules: dict) -> tuple[float, bool]:
    """Срок для объекта скретча: (часы, это_документ).

    Документы и таблицы в скретче — пользовательские материалы (прогон 22:52 удалил
    ug_brand.pdf, tr_koz2.pdf, kz.doc, kz.docx, kz.txt по общему сроку). Для них
    отдельный длинный срок scratch_docs_min_age_days, остальное — как раньше.
    """
    doc = is_doc_entry(p.name) or dir_has_docs(p)
    if doc:
        return rules["scratch_docs_min_age_days"] * 24, True
    return rules["scratch_min_age_hours"], False


def own_uid(p: Path) -> bool:
    """В sticky /tmp чужой uid удалить нельзя — не считаем кандидатом."""
    try:
        return p.lstat().st_uid == os.getuid()
    except OSError:
        return False


# --------------------------------------------------------------------------- классы
def rag_live_generations() -> set[str]:
    """Генерации, которые где-то развёрнуты: их rollback-каталоги не трогаем."""
    gens: set[str] = set()
    candidates = [HH / "skills"]
    candidates += list((HH / "rag-deployments").glob("incremental-*")) if (HH / "rag-deployments").is_dir() else []
    for base in candidates:
        for mf in base.rglob("manifest/build-generation.json"):
            parts = mf.parts
            if any(p.startswith("incremental-rollback-") for p in parts):
                continue
            try:
                val = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            g = val.get("generation") or val.get("semantic_generation")
            if g:
                gens.add(str(g))
    return gens


def gen_of(d: Path) -> str | None:
    mf = d / "manifest" / "build-generation.json"
    try:
        val = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return val.get("generation") or val.get("semantic_generation")


def cands_scratch(rules: dict, now: float) -> list[dict]:
    out = []
    fresh_edge = now - rules["scratch_inner_fresh_hours"] * HOUR
    for prof in sorted((HH / "profiles").glob("*")):
        scratch = prof / "cache" / "scratch"
        if not scratch.is_dir():
            continue
        try:
            entries = sorted(scratch.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_symlink():
                continue
            if e.name in SCRATCH_STOP:
                continue
            try:
                mt = e.stat().st_mtime
            except OSError:
                continue
            age_h, is_doc = scratch_age_hours(e, rules)
            if not old_enough(mt, now, age_h):
                continue
            # guard «нет файла новее N ч» — двусторонний: будущий mtime тоже свежий
            newest = tree_newest(e) if e.is_dir() else mt
            if newest > fresh_edge or newest > now:
                continue
            why = (f"документ/таблица в скретче старше {rules['scratch_docs_min_age_days']} суток"
                   if is_doc else f"прогонная копия {prof.name}")
            out.append({"path": str(e), "bytes": py_size(e), "mtime": round(mt, 3),
                        "age_h": round((now - mt) / HOUR, 1),
                        "min_age_hours": age_h,
                        "class": "scratch", "scratch_kind": "doc" if is_doc else "run",
                        "why": why})
    return out


def cands_tmp_root(root: Path, cls: str, rules: dict, now: float) -> list[dict]:
    """/tmp и /var/tmp: возраст > N ч, двустороннее окно, стоп-лист, свой uid."""
    out = []
    if not root.is_dir():
        return out
    key = "var_tmp_min_age_hours" if cls == "var-tmp" else "tmp_min_age_hours"
    inner_key = "tmp_inner_fresh_hours"
    cutoff_hours = rules[key]
    inner_edge = now - rules[inner_key] * HOUR
    try:
        entries = list(root.iterdir())
    except OSError:
        return out
    for e in entries:
        name = e.name
        if name in TMP_STOP_NAMES or name in STOP_NAMES:
            continue
        if any(name.startswith(pref) for pref in TMP_STOP_PREFIXES):
            continue
        if e.is_symlink():
            continue
        try:
            st = e.lstat()
        except OSError:
            continue
        if st.st_uid != os.getuid():
            continue
        if not e.is_dir() and st.st_nlink > 1:
            continue  # жёсткая ссылка на живой файл в другом месте (только для файлов)
        mt = st.st_mtime
        # Будущий mtime — не «свежий объект», а подделка возраста: он не должен
        # ни удаляться, ни исчезать из отчёта. Раньше такой каталог молча
        # пропускался (ни кандидата, ни отказа) — живой прогон t_513b729f показал,
        # что критерий «отказ с причиной в журнале» не выполнялся.
        newest = tree_newest(e) if e.is_dir() else mt
        future = max(mt, newest) > now
        if not future and not old_enough(mt, now, cutoff_hours):
            continue
        if e.is_dir() and not future and newest > inner_edge:
            continue
        item = {"path": str(e), "bytes": py_size(e), "mtime": round(mt, 3),
                "age_h": round((now - mt) / HOUR, 1),
                "class": cls, "why": f"{root} старше {cutoff_hours:g} ч"}
        if future:
            item["future_mtime"] = round(max(mt, newest), 3)
        out.append(item)
    return out


def cands_rag(rules: dict, now: float) -> list[dict]:
    out = []
    base = HH / "rag-deployments"
    if not base.is_dir():
        return out
    dirs = [d for d in sorted(base.glob("incremental-*")) if d.is_dir() and not d.is_symlink()]
    cut = now - rules["rag_rollback_min_age_days"] * DAY
    live = rag_live_generations()
    keep_doors = set()
    for d in sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)[: rules["keep_rag_rollbacks"]]:
        keep_doors.add(str(d))
    for d in dirs:
        if str(d) in keep_doors:
            continue
        try:
            mt = d.stat().st_mtime
        except OSError:
            continue
        if mt > cut or mt > now:
            continue
        g = gen_of(d)
        if g and g in live:
            continue
        out.append({"path": str(d), "bytes": py_size(d), "mtime": round(mt, 3),
                    "age_h": round((now - mt) / HOUR, 1),
                    "class": "rag-rollback", "why": "поколение нигде не развёрнуто"})
    return out


def cands_backups(now: float) -> list[dict]:
    """Только записи реестра с истёкшим TTL и статусом active."""
    out = []
    reg = HH / "backups-managed" / "registry.json"
    try:
        data = json.loads(reg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    import datetime as _dt
    for e in data.get("entries") or []:
        if e.get("status") != "active":
            continue
        p = e.get("path") or ""
        if not p:
            continue
        created = e.get("created") or ""
        ttl = e.get("ttl_days")
        if not created or not ttl:
            continue
        try:
            born = _dt.datetime.fromisoformat(created)
        except ValueError:
            continue
        age_days = (now - born.timestamp()) / DAY
        if age_days < float(ttl):
            continue
        path = Path(p)
        if path.is_symlink() or not path.exists():
            continue
        try:
            mt = path.stat().st_mtime
        except OSError:
            continue
        out.append({"path": p, "bytes": py_size(path), "mtime": round(mt, 3),
                    "age_h": round(age_days * 24, 1),
                    "class": "backup-ttl", "why": f"TTL {ttl}д истёк ({e.get('id')})",
                    "registry_id": e.get("id")})
    return out


def cands_curator(rules: dict, now: float) -> list[dict]:
    out = []
    base = HH / "skills" / ".curator_backups"
    if not base.is_dir():
        return out
    gens = [d for d in sorted(base.iterdir()) if d.is_dir() and not d.is_symlink()]
    keep = set(str(d) for d in gens[-rules["keep_curator_generations"]:])
    for d in gens:
        if str(d) in keep:
            continue
        try:
            mt = d.stat().st_mtime
        except OSError:
            continue
        out.append({"path": str(d), "bytes": py_size(d), "mtime": round(mt, 3),
                    "age_h": round((now - mt) / HOUR, 1),
                    "class": "curator-backup", "why": "не самое свежее поколение"})
    return out


def cands_hermes_cache(rules: dict, now: float) -> list[dict]:
    """~/.hermes/cache старше N дней, кроме cache/scratch (свой класс) и картинок."""
    out = []
    base = HH / "cache"
    if not base.is_dir():
        return out
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return out
    for e in entries:
        if e.is_symlink() or e.name in HERMES_CACHE_STOP:
            continue
        try:
            mt = e.stat().st_mtime
        except OSError:
            continue
        if not old_enough(mt, now, rules["hermes_cache_min_age_days"] * 24):
            continue
        newest = tree_newest(e) if e.is_dir() else mt
        if newest > now - rules["hermes_cache_min_age_days"] * DAY or newest > now:
            continue
        out.append({"path": str(e), "bytes": py_size(e), "mtime": round(mt, 3),
                    "age_h": round((now - mt) / HOUR, 1),
                    "class": "hermes-cache",
                    "why": f".hermes/cache старше {rules['hermes_cache_min_age_days']} дней"})
    return out


def cands_user_cache(rules: dict, now: float) -> list[dict]:
    """~/.cache: только pip/uv/pnpm/node-gyp/huggingface, старше N дней, свой uid."""
    out = []
    base = HOME / ".cache"
    if not base.is_dir():
        return out
    for name in USER_CACHE_ALLOW:
        d = base / name
        if not d.is_dir() or d.is_symlink():
            continue
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_symlink() or not own_uid(e):
                continue
            if e.name.startswith("."):
                continue  # служебные lock-файлы кэша
            try:
                mt = e.stat().st_mtime
            except OSError:
                continue
            if not old_enough(mt, now, rules["user_cache_min_age_days"] * 24):
                continue
            newest = tree_newest(e) if e.is_dir() else mt
            if newest > now - rules["user_cache_min_age_days"] * DAY or newest > now:
                continue
            out.append({"path": str(e), "bytes": py_size(e), "mtime": round(mt, 3),
                        "age_h": round((now - mt) / HOUR, 1),
                        "class": "user-cache", "why": f"~/.cache/{name} старше "
                                                       f"{rules['user_cache_min_age_days']} дней"})
    return out


def find_tool(name: str) -> str | None:
    cand = shutil.which(name)
    if cand:
        return cand
    if name == "uv":
        roots = [HH / "tools", HOME / ".local" / "bin", Path("/usr/local/bin")]
        for root in roots:
            try:
                if not root.is_dir():
                    continue
                for p in sorted(root.glob("uv-*")):
                    exe = p / "uv"
                    if exe.is_file() and os.access(exe, os.X_OK):
                        return str(exe)
            except OSError:
                continue
    return None


def cands_cache_prune(now: float) -> list[dict]:
    """Предпочитаем штатные prune-команды перед rm в ~/.cache и ~/.hermes/cache."""
    out = []
    uv = find_tool("uv")
    if uv:
        for env in ({"UV_CACHE_DIR": str(HH / "cache" / "uv")}, None):
            p = HH / "cache" / "uv" if env else HOME / ".cache" / "uv"
            if not p.is_dir():
                continue
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            out.append({"path": str(p), "bytes": 0, "mtime": round(mt, 3),
                        "age_h": round((now - mt) / HOUR, 1), "class": "cache-prune",
                        "action": "uv-cache-prune",
                        "cmd": [uv, "cache", "prune"],
                        "env": env or {},
                        "why": f"uv cache prune для {p}"})
    pnpm = find_tool("pnpm")
    if pnpm:
        p = HOME / ".cache" / "pnpm"
        if p.is_dir():
            try:
                mt = p.stat().st_mtime
            except OSError:
                mt = now
            out.append({"path": str(p), "bytes": 0, "mtime": round(mt, 3),
                        "age_h": round((now - mt) / HOUR, 1), "class": "cache-prune",
                        "action": "pnpm-store-prune",
                        "cmd": [pnpm, "store", "prune"],
                        "env": {},
                        "why": "pnpm store prune для ~/.cache/pnpm"})
    return out


def cands_logs(rules: dict, now: float) -> list[dict]:
    """Обрезка логов до N строк (copytruncate: inode сохраняется, аппендер живёт)."""
    out = []
    limit = int(rules["log_max_lines"])
    targets: list[Path] = []
    for pat in (HH / "logs", HH / "profiles"):
        if pat.name == "logs":
            targets += sorted(pat.glob("*.log"))
        else:
            targets += sorted(pat.glob("*/logs/*.log"))
    for f in targets:
        if f.is_symlink() or not f.is_file():
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        # не трогаем только что изменённые файлы — окно потери строк copytruncate
        if st.st_mtime > now - 5:
            continue
        try:
            with f.open("rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if raw.count(b"\n") + (0 if raw.endswith(b"\n") or not raw else 1) <= limit:
            continue
        lines = raw.splitlines(keepends=True)
        if len(lines) <= limit:
            continue
        tail = b"".join(lines[-limit:])
        out.append({"path": str(f), "bytes": st.st_size - len(tail),
                    "mtime": round(st.st_mtime, 3),
                    "age_h": round((now - st.st_mtime) / HOUR, 1),
                    "class": "logs", "action": f"rotate:{limit}",
                    "lines_before": len(lines), "lines_after": limit,
                    "why": f"{len(lines)} строк > {limit}"})
    return out


# --------------------------------------------------------------------------- решатель
def check(cand: dict, live: set[str], cards: dict[str, str]) -> str | None:
    """Возвращает причину отказа или None, если кандидат чист."""
    p = cand["path"]
    path = Path(p)
    if path.is_symlink():
        return "симлинк"
    name = path.name
    if name in STOP_NAMES or name in TMP_STOP_NAMES:
        return "стоп-имя"
    # Двустороннее окно (Z1): будущий mtime = подделка возраста, а не свежесть.
    # Отказ обязан попасть в журнал с причиной — тихий пропуск скрывал бы ровно
    # тот объект, ради которого окно и вводилось (pytest-1630, 2027-01-15).
    if cand.get("future_mtime") is not None:
        return (f"mtime в будущем {cand['future_mtime']:.0f} "
                f"(+{(cand['future_mtime'] - time.time()) / DAY:.0f} дн) — не удаляем")
    for lp in live:
        if under(lp, p):
            return f"живой процесс держит {lp}"
    # Ссылка защищает кандидата только если она ВНУТРИ него (карточка держит
    # конкретный файл/каталог прогона). Упоминание родителя (/var/tmp) не
    # защищает все его подкаталоги — иначе защита парализует любую уборку.
    for cp, cid in cards.items():
        if under(cp, p):
            holder = cid.split(":", 1)[1] if cid.startswith("workspace:") else cid
            return f"ссылка из незакрытой карточки {holder}: {cp}"
    if cand.get("class") == "user-cache":
        parts = path.parts
        if "camoufox" in parts or "ms-playwright" in parts:
            return "браузерный кэш не трогаем"
    return None


def rotate_log(p: Path, limit: int) -> None:
    """copytruncate: читаем, оставляем хвост, пишем на место, обрезаем. Inode сохраняется."""
    path = Path(p)
    with path.open("r+b") as fh:
        raw = fh.read()
        lines = raw.splitlines(keepends=True)
        if len(lines) <= limit:
            return
        tail = b"".join(lines[-limit:])
        if not tail.endswith(b"\n"):
            tail += b"\n"
        fh.seek(0)
        fh.write(tail)
        fh.truncate()
        fh.flush()
        os.fsync(fh.fileno())


def dir_bytes(p: Path) -> int:
    total = 0
    if not p.is_dir():
        return 0
    for root, _d, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def prune_cmd(item: dict) -> tuple[list[str], dict] | None:
    """Команда prune: из манифеста (cmd/env) или восстановленная по action."""
    if item.get("cmd"):
        return list(item["cmd"]), dict(item.get("env") or {})
    act = item.get("action")
    if act == "uv-cache-prune":
        uv = find_tool("uv")
        if not uv:
            return None
        env = {"UV_CACHE_DIR": item["path"]} if item["path"].rstrip("/") == str(HH / "cache" / "uv") else {}
        return [uv, "cache", "prune"], env
    if act == "pnpm-store-prune":
        pnpm = find_tool("pnpm")
        if not pnpm:
            return None
        return [pnpm, "store", "prune"], {}
    return None


def run_prune(item: dict) -> tuple[bool, str]:
    before = dir_bytes(Path(item["path"]))
    got = prune_cmd(item)
    if got is None:
        return False, "команда prune недоступна (нет uv/pnpm)"
    cmd, extra = got
    env = dict(os.environ)
    env.update(extra)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
    except (OSError, subprocess.SubprocessError) as ex:
        return False, f"prune не выполнен: {ex}"
    after = dir_bytes(Path(item["path"]))
    saved = max(0, before - after)
    item["bytes"] = saved
    if proc.returncode != 0:
        return False, f"prune код {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}"
    return True, f"prune ok, освобождено {(saved / MIB):.0f} МиБ"


# --------------------------------------------------------------------------- манифест
def build_manifest(items: list[dict], mode: str, now: float) -> dict:
    return {
        "tool": "disk_reclaim_cron.py",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        "generated_at_epoch": round(now, 3),
        "mode_source": mode,
        "host_free_bytes": shutil.disk_usage("/").free,
        "items": [
            {k: v for k, v in it.items() if k in
             ("path", "class", "bytes", "mtime", "why", "action", "lines_before", "lines_after",
              "scratch_kind", "min_age_hours")}
            | ({"action": "delete"} if it.get("deleted") and not it.get("action") else {})
            for it in items
        ],
        "totals": {
            "items": len(items),
            "bytes": sum(int(it.get("bytes") or 0) for it in items),
        },
    }


def load_manifest(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("в манифесте нет списка items")
    out = []
    for it in items:
        if not isinstance(it, dict) or not it.get("path") or not it.get("class"):
            raise ValueError(f"битая запись манифеста: {it!r}")
        out.append(dict(it))
    return out


def reverify(item: dict, rules: dict, now: float) -> str | None:
    """Перепроверка записи манифеста перед удалением. None = можно."""
    p = Path(item["path"])
    if not (p.exists() or p.is_symlink()):
        return "путь уже отсутствует"
    try:
        mt = p.lstat().st_mtime
    except OSError as ex:
        return f"stat: {ex}"
    tol = float(rules["mtime_tolerance_s"])
    if item.get("mtime") is not None and abs(float(item["mtime"]) - mt) > tol:
        return f"mtime изменился ({mt:.0f} != {item['mtime']})"
    cls = item["class"]
    if cls == "logs":
        limit = int(rules["log_max_lines"])
        try:
            lines = sum(1 for _ in p.open("rb"))
        except OSError as ex:
            return f"чтение: {ex}"
        if lines <= limit:
            return f"уже {lines} строк"
        return None
    if cls == "cache-prune":
        return None
    if cls in ("scratch", "tmp", "var-tmp", "hermes-cache", "user-cache"):
        if cls == "scratch":
            age_h, _is_doc = scratch_age_hours(p, rules)
        else:
            age_h = {"tmp": rules["tmp_min_age_hours"],
                     "var-tmp": rules["var_tmp_min_age_hours"],
                     "hermes-cache": rules["hermes_cache_min_age_days"] * 24,
                     "user-cache": rules["user_cache_min_age_days"] * 24}[cls]
        if not old_enough(mt, now, age_h):
            return f"возраст меньше порога {age_h:g} ч"
        newest = tree_newest(p) if p.is_dir() else mt
        if newest > now:
            return f"внутри будущий mtime {newest:.0f}"
        if cls == "scratch" and newest > now - rules["scratch_inner_fresh_hours"] * HOUR:
            return "внутри файл свежее 2 ч"
        return None
    return None


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-age-hours", type=float, default=None)
    ap.add_argument("--manifest-out", default=None,
                    help="записать манифест кандидатов в PATH")
    ap.add_argument("--card", default=None,
                    help="id карточки, в рамках которой идёт прогон (пишется в журнал kind=run)")
    ap.add_argument("--manifest", default=None,
                    help="при --apply: удалять строго перечисленное в манифесте")
    ap.add_argument("--ignore-card-ref", default=None, action="append",
                    help="id карточки(ек) через запятую, чьи УПОМИНАНИЯ не защищают пути. "
                         "Только для оператора: карточка, которая сама цитирует удаляемый "
                         "объект в теле задачи, иначе запирает его навсегда. Каждое такое "
                         "исключение попадает в журнал (kind=ignore_card_ref).")
    a = ap.parse_args()
    if a.manifest and not a.apply:
        print("disk-reclaim: --manifest требует --apply", file=sys.stderr)
        return 2

    rules = dict(DEFAULT_RULES)
    rules_file = HH / "profiles" / "01-orchestrator" / "scripts" / "disk-reclaim-rules.json"
    if rules_file.is_file():
        try:
            rules.update(json.loads(rules_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    if a.min_age_hours is not None:
        rules["scratch_min_age_hours"] = a.min_age_hours

    now = time.time()
    free_before = shutil.disk_usage("/").free
    mode = "apply" if a.apply else "dry-run"
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        live = live_paths()
    except RuntimeError as ex:
        print(f"disk-reclaim: датчик /proc недоступен ({ex}) — уборка отменена")
        return 2
    db = board_path()
    if db is None:
        print("disk-reclaim: база доски не найдена — уборка отменена")
        return 2
    try:
        cards = open_card_paths(db)
    except sqlite3.Error as ex:
        print(f"disk-reclaim: доска недоступна ({ex}) — уборка отменена")
        return 2
    # Операторское исключение: карточка, которая сама цитирует объект, не должна его запирать.
    ignored = {x.strip() for raw in (a.ignore_card_ref or []) for x in str(raw).split(",") if x.strip()}
    if ignored:
        before = len(cards)
        cards = {p: cid for p, cid in cards.items()
                 if cid.startswith("workspace:") or cid not in ignored}
        journal_run({"ts": started, "kind": "ignore_card_ref", "cards": sorted(ignored),
                     "paths_unprotected": before - len(cards),
                     "note": "сняты только упоминания в тексте; workspace_path сохранён"})

    if a.manifest:
        try:
            cands = load_manifest(Path(a.manifest))
        except (OSError, ValueError) as ex:
            print(f"disk-reclaim: манифест не прочитан ({ex}) — уборка отменена")
            return 2
        source = f"manifest:{a.manifest}"
    else:
        cands = []
        cands += cands_scratch(rules, now)
        cands += cands_tmp_root(Path("/tmp"), "tmp", rules, now)
        cands += cands_tmp_root(Path("/var/tmp"), "var-tmp", rules, now)
        cands += cands_rag(rules, now)
        cands += cands_backups(now)
        cands += cands_curator(rules, now)
        cands += cands_hermes_cache(rules, now)
        cands += cands_user_cache(rules, now)
        cands += cands_cache_prune(now)
        cands += cands_logs(rules, now)
        source = "scan"

    removed, refused = [], []
    budget = rules["max_bytes_per_run"]
    spent = 0
    for c in sorted(cands, key=lambda x: -int(x.get("bytes") or 0)):
        why = check(c, live, cards)
        if not why and a.manifest:
            why = reverify(c, rules, now)
        if why:
            c["refused"] = why
            refused.append(c)
            continue
        if spent + int(c.get("bytes") or 0) > budget and c.get("class") != "logs":
            c["refused"] = "лимит за прогон"
            refused.append(c)
            continue
        if not a.apply:
            c["would"] = True
            removed.append(c)
            spent += int(c.get("bytes") or 0)
            continue
        cls = c.get("class")
        try:
            if cls == "logs":
                rotate_log(Path(c["path"]), int(rules["log_max_lines"]))
                try:
                    c["bytes"] = max(0, int(c.get("bytes") or 0))
                except (TypeError, ValueError):
                    pass
                c["deleted"] = True
                removed.append(c)
                spent += int(c.get("bytes") or 0)
            elif cls == "cache-prune":
                ok, msg = run_prune(c)
                c["result"] = msg
                if ok:
                    c["deleted"] = True
                    removed.append(c)
                    spent += int(c.get("bytes") or 0)
                else:
                    c["refused"] = msg
                    refused.append(c)
            else:
                p = Path(c["path"])
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(c["path"])
                else:
                    p.unlink()
                c["deleted"] = True
                removed.append(c)
                spent += int(c.get("bytes") or 0)
        except OSError as ex:
            c["refused"] = f"ошибка удаления: {ex}"
            refused.append(c)

    rows = [dict(c, ts=time.strftime("%Y-%m-%dT%H:%M:%S%z")) for c in removed + refused]
    journal(rows)

    # структурный журнал: строка на класс
    per_class: dict[str, dict] = {}
    for c in removed:
        d = per_class.setdefault(c["class"], {"class": c["class"], "objects": 0, "bytes": 0,
                                               "refused": 0})
        d["objects"] += 1
        d["bytes"] += int(c.get("bytes") or 0)
    for c in refused:
        d = per_class.setdefault(c["class"], {"class": c["class"], "objects": 0, "bytes": 0,
                                              "refused": 0})
        d["refused"] += 1

    free_after = shutil.disk_usage("/").free
    summary = {
        "checked_at": started,
        "mode": mode,
        "source": source,
        "candidates": removed,
        "refused": refused,
        "freed_bytes": spent,
        "free_before_bytes": free_before,
        "free_after_bytes": free_after,
        "free_gib": round(free_after / GIB, 1),
        "classes": sorted(per_class.values(), key=lambda x: -x["bytes"]),
    }

    journal_run({"ts": started, "kind": "run", "mode": mode, "source": source,
                 "objects": len(removed), "refused": len(refused),
                 "freed_bytes": spent, "free_before_bytes": free_before,
                 "free_after_bytes": free_after,
                 "model": "deepseek-v4-flash", "provider": "letovo",
                 "card": a.card or os.environ.get("HERMES_KANBAN_TASK") or "manual"})
    for d in summary["classes"]:
        journal_run({"ts": started, "kind": "class", "mode": mode, "class": d["class"],
                     "objects": d["objects"], "refused": d["refused"],
                     "mib": round(d["bytes"] / MIB, 1)})
    journal_run({"ts": started, "kind": "summary", "mode": mode,
                 "summary": {k: v for k, v in summary.items() if k not in ("candidates", "refused")},
                 "delta_free_bytes": free_after - free_before})

    if a.manifest_out:
        mf = build_manifest(removed, mode, now)
        mp = Path(a.manifest_out).expanduser()
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps(mf, ensure_ascii=False, indent=1), encoding="utf-8")
        summary["manifest_out"] = str(mp)
        journal_run({"ts": started, "kind": "manifest", "mode": mode, "path": str(mp),
                     "items": mf["totals"]["items"], "bytes": mf["totals"]["bytes"]})
        if not a.json:
            print(f"манифест: {mp} ({mf['totals']['items']} объектов, "
                  f"{mf['totals']['bytes'] / GIB:.2f} ГиБ)")

    if a.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        # Отчёт по ОБЪЁМУ, а не по числу строк: в прогоне 22:52 печатались 43 строки.
        # Заголовок, до 5 крупнейших объектов, «ещё N объектов, см. журнал»; отказы
        # так же. Полный перечень всегда остаётся в logs/disk-reclaim.log.
        TOP = 5
        act = "удалено" if a.apply else "WOULD-DEL"
        print(f"режим {summary['mode']} (источник {source}), свободно {summary['free_gib']} ГиБ; "
              f"удалено {len(removed)} объектов, освобождено {spent/GIB:.2f} ГиБ, "
              f"отказов {len(refused)}")
        if summary["classes"]:
            print("  по классам: " + ", ".join(
                f"{d['class']} {d['objects']}/{d['bytes']/MIB:.1f} МиБ"
                + (f" (+{d['refused']} отказов)" if d["refused"] else "")
                for d in summary["classes"]))
        for c in removed[:TOP]:
            what = "обрезано" if (c["class"] == "logs" and a.apply) else \
                   ("WOULD-ROT" if c["class"] == "logs" else act)
            print(f"  {what} {int(c.get('bytes') or 0)/MIB:9.1f} МиБ [{c['class']}] "
                  f"{c['path']} — {c['why']}")
        if len(removed) > TOP:
            print(f"  ещё {len(removed) - TOP} объектов, см. журнал")
        for c in refused[:TOP]:
            print(f"  оставлено {int(c.get('bytes') or 0)/MIB:9.1f} МиБ [{c['class']}] "
                  f"{c['path']} — {c['refused']}")
        if len(refused) > TOP:
            print(f"  ещё {len(refused) - TOP} отказов, см. журнал")
        print(f"  свободно было {free_before/GIB:.2f} → стало {free_after/GIB:.2f} ГиБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
