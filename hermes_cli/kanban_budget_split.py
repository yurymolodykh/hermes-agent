"""Kanban budget split — automatic fan-out when a worker exhausts its
operation budget.

A kanban card "crashes" on budget exhaustion in two places today:

1. goal_mode workers: ``goals.run_kanban_goal_loop`` hits its turn budget
   (``goal_max_turns``) and blocks the card for a human.
2. classic workers: the agent loop hits ``agent.max_turns`` iterations and
   ``agent.turn_finalizer`` records a ``timed_out`` failure (which counts
   toward the circuit breaker and eventually blocks the card).

This module replaces both dead-ends with an automatic split: the card is
fanned out into 2+ child cards (the ORIGINAL card waits for the graph, same
as a triage fan-out root), so the dispatcher immediately re-runs the work
in smaller pieces. No human in the loop.

How the children are chosen:

* An auxiliary LLM (``auxiliary.kanban_budget_splitter.*``, same client as
  the triage decomposer) sees the card title/body, the worker's last
  progress note, and proposes a 2-6 task graph with dependencies.
* If the LLM is unavailable / malformed / returns junk, a deterministic
  fallback splits into exactly 2 serial cards: "continue remaining work"
  then "finish & verify".
* The atomic DB transition is :func:`kanban_db.split_task_on_budget`
  (children in ``todo`` → parent-free ones promoted to ``ready``, run
  closed with ``outcome='budget_split'``, original card waits on the graph).

Runaway-fan-out guard: ``kanban.budget_split_max_depth`` (default 3) — a
task that already sits that many budget splits deep in its lineage refuses
the split and the caller falls back to the legacy blocked/failure path.
``kanban.budget_split: false`` disables the whole mechanism (legacy
behaviour).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban budget splitter for the Hermes Agent board.

A worker just exhausted its operation budget (turn/iteration limit) on a
kanban card WITHOUT completing it. The card must be split into 2 or more
smaller child cards, each small enough to finish comfortably within one
worker budget. No human is available — decide yourself.

You will be given:
  - The original card title and body
  - The worker's last progress note / partial response (may be empty)

Output a single JSON object with this exact shape:

  {
    "rationale": "<one sentence>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for a FRESH worker with no other context: goal, approach, acceptance criteria. Reference concrete file paths/artifacts from the progress note when relevant.>",
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - Exactly 2 to 4 tasks. Each must be strictly smaller than the original.
  - EXCEPTION — the close-only case: when the progress note states that the
    work is actually FINISHED and only the card is still open (artifact
    produced, tests run, nothing left to implement), do NOT decompose it.
    Output exactly ONE task titled "verify and close: <title>" whose body says
    the artifact already exists and must NOT be redone. Re-splitting finished
    work into 2-4 fresh workers is the failure this rule exists to stop.
  - "parents" is a list of 0-based INDICES into this same "tasks" list.
    Tasks with no parents run in PARALLEL. Tasks with parents wait until
    every parent completes.
  - Preserve unfinished work from the progress note: the first child should
    pick up where the worker stopped, not restart from scratch.
  - The children together must cover ALL remaining work of the original card.
  - Keep the assignee implied by the card: each child runs under the same
    profile, so do not invent cross-profile handoffs.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Worker's last progress note (partial, budget ran out mid-work):
{progress}

Rule that overrides everything above: if the progress note says the work is
actually FINISHED and only the card is still open (artifact built, tests run,
only a verify-and-close step left), do NOT split it into parts — output
EXACTLY ONE task titled "verify and close: <title>" that re-uses the finished
artifact and does not redo it. Otherwise output 2-4 tasks as specified.
"""

# A note resolves to the single "verify and close" card only when it names a
# CLOSING REMAINDER — the only thing still open is the card itself.
#
# Bare completion phrasing («работа выполнена», «всё сделано», «already
# completed») is deliberately NOT sufficient. It is the normal way a worker
# OPENS a partial-credit summary («работа выполнена частично, осталось
# доделать раздел 3»), so treating it as close-only shipped children whose body
# said both «осталось доделать раздел 3» and «ALREADY DONE — DO NOT redo», and
# the remainder got no card at all (review of 15743759b0: 11 of 22 realistic
# partial-credit tails collapsed; the pre-fix revision split them correctly).
_CLOSE_ONLY_NO_WORK_LEFT = (
    "nothing left to implement",
    "nothing left to do",
    "nothing left to change",
    "only the card is still open",
    "only the card remains",
    "переделывать не нужно",
    "не нужно переделывать",
)

# «осталось (только|лишь)? закрыть» ENDING its clause, optionally followed by
# the card artifact as the LAST word of that clause.
#
# What counts as the end of the clause: sentence punctuation (`.!?…`), a
# closing bracket, a quote, or end of text. `:`, `;` AND a line break are
# deliberately EXCLUDED:
#
# * `:`/`;` introduce an enumeration of what still remains («осталось закрыть:
#   раздел 3, тесты») — the single most common way a worker lists unfinished
#   work. Accepting them collapsed 6 of 14 realistic mid-work tails in review of
#   c2338e94d1.
# * a line break is where a worker starts that same list on the next line
#   («Осталось закрыть\n- раздел 3\n- тесты»). Accepting it (review 23b6af0536)
#   collapsed both bullet-list regressions and dropped «раздел 3» / «тесты»
#   entirely. A trailing newline is still fine: `\s*` swallows it before the
#   end-of-text anchor.
#
# Deliberately NOT accepted: closing a WORK ITEM — «осталось закрыть раздел 3»,
# «осталось закрыть миграции», «осталось закрыть тестами функцию parse()»,
# «осталось закрыть вопрос с лицензией», «осталось закрыть задачу миграции на
# прод». The artifact word («карточку / тикет / задачу») only counts when
# nothing is attached to it inside the same clause: «осталось закрыть карточку
# и раздел 3» keeps working because the clause continues past «карточку».
# The asymmetry is intentional: a false positive drops unfinished work with no
# card at all, while a false negative only costs one redundant split.
_CLOSE_REMAINDER_RE = re.compile(
    r"осталось\s+(?:только\s+|лишь\s+)?закрыть"
    r"(?:\s+(?:карточк[уа]|тикет[а]?|задач[у]))?"
    r"[^\S\n]*(?:[.!?…)}\»\"']|$)",
    re.IGNORECASE,
)

# A later clause that RE-OPENS the remainder («Осталось закрыть.
# Осталось: раздел 3 и тесты», «осталось закрыть карточку. Но осталось:
# раздел 3»), and a partial-credit remainder stated BEFORE the closer
# («Осталось: раздел 3. Осталось закрыть карточку», «Раздел 3 не начат.
# Осталось закрыть карточку») both mean the card is not the only thing left.
_CLOSE_REMAINDER_REOPENS_RE = re.compile(
    # «Осталось:» / «Осталось закрыть: раздел 3» / «Остались: тесты» — a
    # remainder introduced by a colon within a short window after «остал(о|и)сь».
    r"остал[оа]сь\b[^.\n]{0,40}?[:;]",
    re.IGNORECASE,
)
# Work-naming verb immediately before the closer («надо дописать, осталось
# закрыть») is not required to veto — the negations/partial markers cover the
# explicit forms; the two patterns above cover the structural ones.

# Negation and partial-credit qualifiers disqualify the note OUTRIGHT, checked
# before any positive signal: a note that says the work is not finished must
# never resolve to the close-only card, even when a positive marker appears in
# the same text.
_CLOSE_ONLY_NEGATIONS = (
    "не полностью",
    "не выполнена",
    "не выполнено",
    "не готов",
    "не готова",
    "не готово",
    "не закончена",
    "не закончено",
    "не завершена",
    "не завершено",
    "не всё сделано",
    "не все сделано",
    "не всё готово",
    "не все готово",
    "ещё не готово",
    "еще не готово",
    "not complete",
    "not fully",
    "not finished",
    "not done",
    "incomplete",
    "still remaining",
)

# Partial-credit qualifiers. Each of these says "some of it is done" — which is
# the opposite of close-only — and they are the qualifier class the previous
# revision missed («частично / на 70% / в основном / кроме / наполовину»).
_CLOSE_ONLY_PARTIAL_MARKERS = (
    "частично",
    "наполовину",
    "в основном",
    "не до конца",
    "кроме",
    "за исключением",
    "в работе",
    "не начат",
    "не начато",
    "не написано",
    "не написаны",
    "не реализовано",
    "не реализован",
    "не подключ",
    "не проверял",
    "черновик",
    "первую половину",
    "осталось доделать",
    "осталось дописать",
    "осталось написать",
    "осталось реализовать",
    "осталось починить",
    "надо дописать",
    "надо починить",
    "требует решения",
    "not started",
    "in progress",
    "partly",
    "partially",
    "still open for part",
)
# «на 70%», «70 %» — a numeric share below 100 means partial. 100% is not
# matched on purpose (it is not a partial-credit signal).
_CLOSE_ONLY_PARTIAL_PERCENT_RE = re.compile(r"(?:^|[^\d])[1-9]?\d\s*%")

# ---------------------------------------------------------------------------
# Substantive tail after the closing statement
# ---------------------------------------------------------------------------
# Review 23b6af0536 (r4, item 2): the closer only closes the card when nothing
# substantive follows it. The collector appends the run's last narration AFTER
# the post-budget summary, so on a real seam the note reads
#
#     Что воркер сообщил при остановке (итог прогона):
#     Осталось закрыть карточку.
#
#     Последнее, что воркер успел сказать (хвост ответа):
#     Код готов, тесты не писал.
#
# and the closer's clause "ends" right before a section that names open work.
#
# Polarity, stated plainly: a tail vetoes only when it NAMES work that is still
# open (or opens a list of it). Naming the tail as a whole "substantive" is not
# usable as the test, because the collector also appends the run's last
# narration — which on a genuinely finished run is ordinary earlier narration
# («Начал работу.») or a completion recap («Собрал отчёт, прогнал тесты, всё
# зелёное.»), and both must keep the single card (reviewer r4 s2 control).
# What must never survive is a tail that says something is still missing.
#
# Review r5 of ec0b6d3e74 (blocking): the veto only recognised LONG-form
# participles («не проверен», «не написано») and -л past verbs. Russian reports
# state the same remainder with SHORT-form passives («раздел 3 не тронут»,
# «тестирование не тронуто», «разделы 3 и 4 не тронуты», «раздел 4 не покрыт»),
# with absence predicates («тесты отсутствуют»), with requirement predicates
# («раздел 3 требует доработки», «для сборки не хватает раздела 3»), with
# «без <чего>» («остался без покрытия»), with «не проводил(ся/ась)» («проверка
# не проводилась») and with «не всё» after the participle («покрыто не всё»,
# «завершено не всё»). All 13 shapes collapsed to a single «ALREADY DONE» card
# and the named remainder got no card at all — the exact work-loss this module
# exists to prevent. Every extension below is additive: no existing branch was
# weakened, and the close-only controls stay green (see
# tests/hermes_cli/test_kanban_budget_split_progress.py).
_TAIL_REMAINDER_RE = re.compile(
    r"\bне\s+[а-яё]+(?:л|ла|ло|ли)\b"      # не писал / не сделал / не успел
    r"|\bне\s+[а-яё]+(?:н|на|но|ны)\b"     # не проверен / не написано
    r"|\bне\s+[а-яё]+(?:т|то|ты)\b"        # не тронут / не покрыто / не тронуты
    r"|\bне\s+(?:трогал|касался)"          # раздел 3 не трогал
    r"|остал[аои]?сь|оста[её]тся"           # осталось / остались / остаётся
    r"|\bнет\b"                            # тестов нет
    r"|предстоит|в\s+очереди|не\s+доделан|доделать|дописать|вис(?:ит|ят)"
    r"|сначала|дальше|потом|завтра|затем"
    # absence / requirement / «без <чего>» predicates — the remainder is stated
    # without a negated verb, so none of the branches above see it.
    r"|отсутству"                          # тесты отсутствуют
    r"|не\s+хватает|недоста[её]т"          # для сборки не хватает раздела 3
    r"|требу(?:ет|ют)"                     # раздел 3 требует доработки
    r"|без\s+(?:покрыти|тестов|проверк|доработк|завершени|реализаци|сборки)"
    r"|не\s+проводил"                      # проверка не проводилась
    # «не всё» after a participle: «покрыто не всё», «завершено не всё».
    r"|не\s+вс(?:ё|е)\b"
    r"|\btodo\b|\bpending\b|\bremaining\b|\bnext\b",
    re.IGNORECASE,
)
# A tail line that opens a list item is the enumeration of what remains
# («Осталось закрыть.\n- раздел 3\n- тесты»).
_TAIL_LIST_ITEM_RE = re.compile(r"^(?:[-*•+]|\d+[.)])\s+\S")

# The collector's own service lines (see
# :func:`agent.turn_finalizer._collect_budget_progress_note`). They are appended
# by the seam, not written by the worker, so they never make a tail substantive.
_COLLECTOR_LABEL_PREFIXES = (
    "выполнено ранее (не переделывай)",
    "остановка по бюджету:",
    "что воркер сообщил при остановке",
    "последнее, что воркер успел сказать",
)
_COLLECTOR_FILES_PREFIX = "файлы, созданные/изменённые"
# The one collector section whose content is EARLIER narration, not the run's
# closing statement. Its content is separated from the worker body (see
# :func:`_split_note`) because an opening line («Начал работу.») says nothing
# about what remains — feeding it to the completion whitelist would split every
# genuinely finished run. It is still scanned as veto-only evidence.
_TRANSCRIPT_LABEL_PREFIX = "последнее, что воркер успел сказать"


def _split_note(text: str) -> tuple[str, str]:
    """``text`` split into ``(worker_body, closing_transcript_tail)``.

    The collector (:func:`agent.turn_finalizer._collect_budget_progress_note`)
    appends its own labelled sections and a ``- path`` inventory. Those lines are
    seam-generated, not worker-written, so they must never be read as evidence
    about remaining work. Worker-written bullets are kept — they are list items
    of the remainder.

    Two sections are worker-written, and they are NOT equivalent:

    * «Что воркер сообщил при остановке (итог прогона)» — the post-budget
      summary, generated after the budget ran out. This is the only text that
      reliably states what REMAINS, so it stays in ``worker_body`` and is graded
      by the completion whitelist.
    * «Последнее, что воркер успел сказать (хвост ответа)» — the last narration
      row, i.e. a step EARLIER than that summary («Начал работу.»). It cannot
      argue that work is still open on its own strength, so it is returned
      separately: it only ever VETOES (see
      :func:`_progress_reports_completion`), never grants the single card.
    """
    kept: list[str] = []
    tail: list[str] = []
    in_files = False
    in_transcript = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            in_files = False
            continue
        low = stripped.lower()
        if low.startswith(_COLLECTOR_FILES_PREFIX):
            in_files = True
            in_transcript = False
        elif in_files and stripped.startswith("- "):
            continue
        elif any(low.startswith(p) for p in _COLLECTOR_LABEL_PREFIXES):
            in_transcript = low.startswith(_TRANSCRIPT_LABEL_PREFIX)
            in_files = False
            continue
        elif in_transcript:
            tail.append(stripped)
        else:
            in_files = False
            kept.append(stripped)
    return "\n".join(kept), "\n".join(tail)


def _worker_lines(text: str) -> str:
    """The worker-written body of the note, service lines and transcript tail removed."""
    return _split_note(text)[0]


def _tail_names_open_work(tail: str) -> bool:
    """True when the text after the closer still says something is missing.

    Clauses that :func:`_residual_is_completion` already recognises as full
    completion statements are removed before the blacklist scan. That is not a
    weakening: the blacklist keys on single words («нет», «осталось», «требует»,
    «без»), and a genuine completion can contain those words — «замечаний нет»,
    «129 тестов проходят, провалов нет». Without the filter every such phrase
    vetoed a finished run and produced a redundant split. Text that is NOT a
    recognised completion is scanned unchanged, so «…, провалов нет. Раздел 3
    недописан.» still vetoes through the second clause.
    """
    if not tail:
        return False
    kept: list[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _residual_is_completion(stripped):
            continue
        kept.append(stripped)
    if not kept:
        return False
    scan = "\n".join(kept)
    if _TAIL_REMAINDER_RE.search(scan):
        return True
    return any(_TAIL_LIST_ITEM_RE.match(line) for line in kept)


# ---------------------------------------------------------------------------
# Completion whitelist — the polarity switch (review r6 of 341f55734d)
# ---------------------------------------------------------------------------
# Every previous round extended ``_TAIL_REMAINDER_RE``, a BLACKLIST: a note was
# split only when a remainder word was recognised. The blacklist always leaks —
# r4 enumerated 13 shapes, r5 another 13, and the reviewer's fresh r6 corpus
# still collapsed 15/24 untouched shapes («раздел 3 недописан», «ждёт
# проверки», «нужна доработка», «заглушка», «0 тестов», «позже» …) into ONE
# «ALREADY DONE — DO NOT redo» card while the named remainder got no card at
# all. That is the exact work-loss this module exists to prevent.
#
# The polarity is therefore inverted here: a close-only card is emitted only
# when the worker-written residual is EMPTY or consists exclusively of
# recognised completion statements. Anything unrecognised goes back to a real
# split. The asymmetry is deliberate and matches the module's documented risk
# model: a redundant split costs one extra card, a leak costs the work.
#
# Cost of the switch: a genuinely finished run whose closing summary words the
# completion in a way not listed below splits into two cards. That is a cheap,
# self-correcting failure (the second child verifies and closes), and it keeps
# the r6 corpus and every later word-shape safe by construction instead of by
# enumeration.
_COMPLETION_CLAUSES = [
    # no-work-left statements (the same phrases the span remover cuts out)
    re.compile(
        r"(?:переделывать\s+не\s+нужно|не\s+нужно\s+переделывать"
        r"|nothing\s+left\s+to\s+\w+|only\s+the\s+card\s+(?:remains|is\s+still\s+open)"
        r"|everything\s+landed|all\s+done)",
        re.IGNORECASE,
    ),
    # the collector's own header echo, if it ever survives line filtering
    re.compile(r"перечисленное\s+ниже.*", re.IGNORECASE),
    # «всё готово / всё собрано / всё зелёное / всё на месте»
    re.compile(
        r"(?:вс[её]|все)\s+(?:уже\s+)?(?:готово|собрано|сделано|проверено"
        r"|прогнано|написано|зел[её]н\w*|на\s+месте|в\s+порядке|работает"
        r"|проходит|пройдено|покрыто)(?:\s+.*)?",
        re.IGNORECASE,
    ),
    # «артефакт собран», «отчёт готов», «тесты зелёные», «работа выполнена
    # (полностью)», «сборка проходит» — object first, participle second.
    re.compile(
        # an optional quantifier belongs to the object: «все разделы на месте»,
        # «оба файла готовы», «все тесты зелёные».
        r"(?:все\s+|всё\s+|оба\s+|обе\s+)?"
        r"(?:артефакт\w*|отч[её]т\w*|отчет\w*|файл\w*|(?:[а-яё]+-)?тест\w*|код|сборк\w*"
        r"|документ\w*|скрипт\w*|модул\w*|патч\w*|данн\w*|страниц\w*"
        r"|презентац\w*|карточк\w*|задач\w*|работ\w*|раздел\w*|миграц\w*"
        r"|pytest|ruff|mypy|\d+\s+тест\w*)"
        # an enumerated range may sit between the object and its participle
        # («Разделы 1-4 собраны…», «Тесты 12, 13, 14 зелёные»).
        r"(?:\s*[\d,–—-]+)*"
        r"\s+(?:(?:уже|полностью|целиком|весь|все|вс[её])\s+)*"
        r"(?:готов[аоы]?|собран[аоы]?|написан[аоы]?|проверен[аоы]?"
        r"|зел[её]н\w*|пройден\w*|прогнан\w*|сделан[аоы]?|закончен[аоы]?"
        r"|выполнен[аоы]?|опубликован\w*|закоммичен\w*|на\s+месте|в\s+порядке"
        # a conjunctive/adverbial beat may sit between the participle and the
        # location predicate: «Все разделы написан и на месте».
        r"|(?:написан\w*|собран\w*|сделан\w*)\s+(?:и|да|тоже)\s+на\s+месте)"
        # what follows is the object's location/scope, not a second predication:
        # «Отчёт готов: docs/weekly/report.v2.md», «Артефакт собран — reports/x.md».
        # A real second predication is cut out earlier by _CLAUSE_LINK_SPLIT_RE
        # (brackets, «но», «кроме», «а»), which is why this tail stays permissive.
        r"(?:\s*[:—–]\s*\S.*|\s+.*)?",
        re.IGNORECASE,
    ),
    # «готово», «сделано», «готово: разделы 1-4, тесты зелёные»
    re.compile(r"(?:вс[её]\s+)?(?:готово|сделано|done|landed|completed)(?:\s*:.*)?",
               re.IGNORECASE),
    # «собрал X», «прогнал тесты», «проверил Y» — a completed-work clause
    re.compile(
        r"(?:я\s+)?(?:собрал\w*|сделал\w*|написал\w*|проверил\w*|прогнал\w*"
        r"|закончил\w*|закрыл\w*|починил\w*|исправил\w*|разобрал\w*"
        r"|обновил\w*|переписал\w*|протестировал\w*|выполнил\w*|завершил\w*"
        r"|добавил\w*|залил\w*|закоммитил\w*|опубликовал\w*|развернул\w*"
        r"|сбилдил\w*)(?:\s+.*)?",
        re.IGNORECASE,
    ),
    # English completion statements
    re.compile(
        r"(?:the\s+)?work\s+is\s+(?:already\s+)?(?:done|complete[d]?)(?:\s+.*)?"
        r"|already\s+(?:done|completed|implemented|landed)(?:\s+.*)?",
        re.IGNORECASE,
    ),
    # test counters: «129 тестов зелёные», «129 passed», «129 тестов проходят,
    # pytest 8.2» — a trailing tool/version annotation is part of the statement.
    re.compile(r"\d+\s+(?:[а-яё]*тест\w*\s+)?"
               r"(?:passed|pass|прошл\w*|проход\w*|зел[её]н\w*|пройден\w*|прогнан\w*)"
               r"(?:\s*[,;—–]\s*\S.*)?",
               re.IGNORECASE),
    # «документация обновлена», «проблема устранена», «патч применён» — a noun
    # followed by a Russian SHORT passive participle is a completion statement.
    # Guarded by the negation check in :func:`_clause_is_completion`, so
    # «раздел 3 недописан» / «раздел 3 не проверен» can never take this branch.
    re.compile(
        r"(?:[а-яё\w]+\s+){1,4}"
        r"(?:обновл[её]н\w*|устран[её]н\w*|исправлен\w*|добавлен\w*|внес[её]н\w*"
        r"|измен[её]н\w*|переписан\w*|починен\w*|установлен\w*|настроен\w*"
        r"|интегрирован\w*|реализован\w*|подключ[её]н\w*|удал[её]н\w*"
        r"|перенес[её]н\w*|написан\w*|собран\w*|проверен\w*|прогнан\w*"
        r"|сделан\w*|закончен\w*|выполнен\w*|опубликован\w*|закоммичен\w*"
        r"|запушен\w*|зел[её]н\w*)",
        re.IGNORECASE,
    ),
]
# An explicit "nothing to redo" phrase is a completion statement even though it
# contains «не»; checked BEFORE the blanket negation guard below. The same goes
# for "no defects found" — «замечаний нет», «ошибок не обнаружено» — which is a
# completion statement phrased as a negation of a DEFECT noun. The defect noun
# is required, so this can never excuse «раздела 3 нет».
_NO_REDO_CLAUSE_RE = re.compile(
    r"(?:переделывать\s+не\s+нужно|не\s+нужно\s+переделывать"
    r"|nothing\s+left\s+to\s+\w+|redoing\s+is\s+not\s+needed)",
    re.IGNORECASE,
)
_NO_DEFECT_CLAUSE_RE = re.compile(
    r"(?:замечан\w*|ошиб\w*|проблем\w*|дефект\w*|падени\w*|провал\w*|баг\w*)"
    r"(?:\s+\S+){0,3}?\s+(?:нет|не\s+(?:обнаружен\w*|найден\w*|выявлен\w*|воспроизв\w*))"
    r"|(?:no|zero)\s+(?:failures?|errors?|defects?|issues?)",
    re.IGNORECASE,
)
# Any negation anywhere in a clause means it is NOT a clean completion
# statement — a remainder stated as a negated participle («раздел 3 недописан»,
# «тесты не проверены») must never be granted the single card.
_CLAUSE_NEGATION_RE = re.compile(r"\bне\b|\bне[а-яё]|\bnot\b|\bno\b|\bwithout\b",
                                 re.IGNORECASE)
_CLAUSE_EDGE_STRIP = " \t.,:;!?…\"'()[]{}«»-–—"
# A sentence ends at terminal punctuation FOLLOWED BY SPACE OR END OF TEXT. The
# lookahead matters: worker notes are full of file paths, versions and decimals
# («reports/full.md», «pytest 8.2, 129 passed»), and a naive split on «.» cut
# «разделы 1-4 собраны в reports/full.md, 129 тестов проходят» into
# «…reports/full» + «md, 129 тестов проходят», i.e. produced two fragments that
# are no longer recognised completion clauses and forced a redundant split on a
# genuinely finished run.
_SENTENCE_SPLIT_RE = re.compile(r"[!?…;\n]+|\.\s+|\.$")
# A completion pattern may END with a short tail («собраны в reports/full.md»),
# and that tail can hide a SECOND predication: «Отчёт собран (раздел 3 пока
# отсутствует).» matched the object+participle pattern wholesale and was granted
# the single card although a parenthetical names open work. Rather than
# blacklist the words inside a tail (which would re-introduce the enumeration
# treadmill this module just escaped), the clause is cut at the STRUCTURAL
# boundaries that introduce a second predication — brackets and adversative
# conjunctions — and every fragment must independently be a completion
# statement. This is a widening of the whitelist, not a list of remainder words:
# any new phrasing still fails closed, because an unrecognised fragment forces a
# split. «кроме»/«за исключением» split for the same structural reason (a
# subtraction is a second predication), so «готово всё, кроме приложения Б»
# fails closed exactly as before.
_CLAUSE_LINK_SPLIT_RE = re.compile(
    r"[()\[\]{}]"
    r"|\b(?:но|однако|кроме|за\s+исключением)\b"
    r"|\bа\b(?!\s+также)",
    re.IGNORECASE,
)
_ATOM_EDGE_STRIP = " \t.,:;!?…\"'«»-–—"


def _clause_is_completion(clause: str) -> bool:
    """True when the whole clause is a recognised completion statement."""
    text = clause.strip(_CLAUSE_EDGE_STRIP)
    if not text:
        return True
    if _NO_REDO_CLAUSE_RE.search(text):
        return True
    if _NO_DEFECT_CLAUSE_RE.fullmatch(text):
        return True
    if _CLAUSE_NEGATION_RE.search(text):
        return False
    return any(pattern.fullmatch(text) for pattern in _COMPLETION_CLAUSES)


def _atom_is_completion(atom: str) -> bool:
    """A single predication, after structural link-splitting and comma chaining."""
    atom = atom.strip(_ATOM_EDGE_STRIP)
    if not atom:
        return True
    if _clause_is_completion(atom):
        return True
    parts = [p for p in atom.split(",")]
    return all(_clause_is_completion(p) for p in parts)


def _residual_is_completion(residual: str) -> bool:
    """True only when EVERY clause of the residual is recognised completion.

    This is the whitelist gate that makes the close-only decision fail SAFE:
    an unrecognised clause anywhere in what the worker wrote returns ``False``,
    which means "split" — the remainder keeps a card of its own.
    """
    for sentence in _SENTENCE_SPLIT_RE.split(residual):
        sentence = sentence.strip(_CLAUSE_EDGE_STRIP)
        if not sentence:
            continue
        # Commas chain completion clauses («Работа выполнена, артефакт готов»),
        # and brackets / «но» / «кроме» introduce a SECOND predication that must
        # also be a completion («Отчёт собран (раздел 3 пока отсутствует)»
        # fails here). A part that names open work forces a split.
        for atom in _CLAUSE_LINK_SPLIT_RE.split(sentence):
            if _atom_is_completion(atom):
                continue
            return False
    return True


# ---------------------------------------------------------------------------
# Decisive open-work test — the r8 grant layer (review r7 of b76aad825f)
# ---------------------------------------------------------------------------
# r7 replaced the blacklist with a completion WHITELIST, which mirrored the
# defect instead of removing it: the single close-only card was granted only
# when EVERY clause was a recognised completion statement, so an unrecognised
# word-form sent finished work back for rework. Measured on the reviewer's own
# corpora: 37/48 ordinary finished summaries, 7/8 real-seam shapes and 100%
# (467/467) of live completed runs over-split, against 6% / 12% on the
# pre-polarity baseline.
#
# The decisive test is not "is each clause a completion statement" but the
# question the card actually asks: does anything the worker wrote NAME work that
# is still open? Three layers:
#
#   L1 EXCUSE  — a clause that negates a DEFECT, reports a zero count of a bad
#                thing, or says «переделывать не нужно» is a POSITIVE completion
#                signal, whatever else it contains.
#   L2 DETECT  — open work is a clause whose predicate says the work has not
#                landed: a negated achievement verb (both word orders, incl.
#                reflexives), a fused negation («недописан»), a future/modal
#                plan («напишу», «надо дописать», «предстоит»), a remainder
#                predicate («осталось», «требует», «отсутствует»), a waiting or
#                blocked STATE («на проверке», «заблокирован»), a partiality
#                that states PROGRESS («готовность 60%», «3 из 8»), or an
#                emptiness predicated on the artifact («тестов нет», «пустой»).
#   L3 GRANT   — one card iff no clause of the worker-written residual is an L2
#                open-work clause AND the residual is not a bare enumeration.
#
# Two structural vetoes survive from before the polarity switch, because they
# are about SHAPE and no wording list can express them:
#   * a worker-written bullet/numbered line after the closer enumerates what
#     remains (r4 review, item 2);
#   * a residual line that is nothing but a short list of things, with no
#     predicate anywhere («раздел 3, тесты, схема»), names work.
#
# A bare percentage is NOT partiality: finished runs report coverage, speedups
# and deltas («покрытие 91%», «на 100%»). Only «готовность N%», «N% работы» and
# «N из M» with N < M state progress.
_DEFECT_NOUN = (
    r"(?:замечан\w*|ошиб\w*|проблем\w*|дефект\w*|падени\w*|провал\w*|баг\w*"
    r"|расхожден\w*|регресс\w*|warning\w*|failure\w*|error\w*|issue\w*)"
)
_NO_REDO_L1 = re.compile(
    r"(?:переделывать|переделать)\s+не\s+(?:нужно|надо|требуется|придётся|придется)"
    r"|не\s+нужно\s+переделывать"
    r"|(?:nothing\s+left\s+to|redoing\s+is\s+not\s+needed|no\s+need\s+to\s+redo)"
    + r"|(?:" + _DEFECT_NOUN + r")(?:\s+\S+){0,2}?\s+(?:нет|0|не\s+(?:обнаружен\w*"
    r"|найден\w*|выявлен\w*|воспроизв\w*))"
    + r"|\b0\s+(?:failed|failures?|errors?|warning\w*)"
    # Zero counts of BAD things: «0 пропущено, 0 задвоено», «0 wip markers
    # staged», «ни одного удаления», «без пустых страниц». Measured on the live
    # board as over-splits that carried no actual open work.
    + r"|\b0\s+(?:пропущ\w*|задво\w*|ошиб\w*|замечан\w*|wip\b|markers?\b"
    r"|дубл\w*|удален\w*|удалён\w*|дефект\w*|провал\w*|страниц\w*)"
    + r"|\bни\s+одного\s+(?:удален\w*|удалён\w*|провал\w*|расхожден\w*|дефект\w*)"
    + r"|\bбез\s+пуст\w*"
    + r"|\b(?:no|zero)\s+(?:failures?|errors?|defects?|issues?|warnings?)"
    r"|без\s+(?:" + _DEFECT_NOUN + r"|предупрежден\w*)",
    re.IGNORECASE,
)

_PRED_STEMS = (
    r"напис\w*|собр\w*|сдела\w*|провер\w*|прогон\w*|прогна\w*|законч\w*|заверш\w*"
    r"|закры\w*|почин\w*|исправ\w*|доработ\w*|додела\w*|трога\w*|трону\w*"
    r"|каса\w*|покры\w*|покро\w*|наполн\w*|перенес\w*|перенесён\w*|обновл\w*"
    r"|добавл\w*|реализ\w*|подключ\w*|установл\w*|настро\w*|развёрн\w*"
    r"|разверн\w*|запуш\w*|закоммит\w*|залив\w*|залил\w*|протестир\w*"
    r"|отревью\w*|ревью\w*|оцен\w*|запуск\w*|запуска\w*|тестир\w*"
    r"|писа\w*|писал\w*|стро\w*|сда\w*|сдан\w*|включ\w*|выполн\w*"
    # requirement / conformance: «лимит не соблюдён», «не подтверждена
    # тестами», «раздел пропущен», «не начат», «не прошёл».
    r"|нача\w*|начин\w*|пропущ\w*|опущ\w*|подтвержд\w*|соблюд\w*|влез\w*"
    r"|соответств\w*|выдерж\w*|прош\w*|проход\w*|зачтён\w*|зачтен\w*"
    # «не успел дописать», «проверка не проводилась», «не довёл до конца» —
    # achievement verbs whose negation is the remainder itself, not a defect.
    # `провод[ия]` (not `провод`) keeps the NOUN «провод» out of the predicate set.
    r"|успе\w*|допис\w*|провод[ия]\w*|довёл\w*|довел\w*|довод\w*|дожа\w*"
)
_OPEN_WORK_L2 = re.compile(
    # (1) negated achievement predicate, both word orders, incl. reflexives
    r"\bне\s+(?:\S+\s+){0,2}?(?:" + _PRED_STEMS + r")"
    r"|\b(?:" + _PRED_STEMS + r")\s+не\s+(?:был\w*|было|были|была|успе\w*|смог\w*)"
    # (2) fused negation: недописан / недоделан / незакрыт / неготов
    r"|\b(?:недопис|недодел|недосда|недосдан|незакрыт|незаверш|неготов|незаконч"
    r"|неотревь|неоцен|непровер|ненапис|несобран|нереализов|неподключ|неисправ)\w*"
    # (3) future / modal / plan. «следует» only before an infinitive: «чистый ДВС
    # следует за винтом» is "follows", not "should".
    r"|\b(?:напиш|сдела|добавл|додела|доработ|уточн|законч|заверш|исправ|перепиш"
    r"|провер|разбер|собер|закро|почин)(?:у|ю|им|ем|ишь|ите|ет|ут|ют|ат|ят)\b"
    r"|\bследует\s+[а-яё]+(?:ть|ться|ти)\b"
    r"|\b(?:надо|нужно|нужна|нужен|нужны|требуется|предстоит|планир\w*|запланир\w*"
    r"|отложен\w*|в\s+дальнейшем|позже|потом|завтра|следующ\w*|очередн\w*)\b"
    # (4) waiting / blocked / state predicates
    r"|\b(?:ждём|ждем|ждет|ждёт|ждут|ожида\w*|заблокирован\w*|приостановл\w*"
    r"|в\s+очереди|в\s+процессе|в\s+работе|на\s+проверке|на\s+согласовании"
    r"|проверяется|согласуется|дописывается|дорабатывается"
    r"|заготовк\w*|заглушк\w*|каркас\w*|скелет\w*|набросок\w*|черновик\w*|чернов\w*)\b"
    # (5) remainder predicates stated affirmatively
    r"|\b(?:остал\w*|остаётся|остается|остава\w*|требу\w*|вис(?:ит|ят)"
    r"|не\s+хватает|недостаёт|недостает|отсутству\w*)\b"
    # (6) absence
    r"|\bнет\b|\bотсутствует\b|\bотсутствуют\b"
    # (7) partiality that states PROGRESS, plus absence-with-«без» of a work noun
    r"|\bполовин\w*|\bнаполовин\w*|\bчастичн\w*|\bpartially\b|\bpartly\b"
    r"|\bбез\s+(?:тест\w*|покрыти\w*|проверк\w*|доработк\w*|завершени\w*"
    r"|реализаци\w*|сборк\w*|согласован\w*|ревью)"
    r"|\bготовность\b"
    r"|(?:на|готовность|выполнено\s+на|сделано\s+на)\s+(?:[1-9]|[1-9]\d)\s*%"
    r"|\b(?:[1-9]|[1-9]\d)\s*%\s*(?:работ|готов)"
    r"|\bв\s+основном\b|\bне\s+всё\b|\bне\s+все\b|\bне\s+до\s+конца\b"
    # (7b) EXCLUSIVE connectives: a completion followed by «кроме»/«за
    # исключением»/«однако» names what the completion does NOT cover
    # («Тесты зелёные, кроме интеграционных»). Judged per atom, never as a
    # blanket veto over the whole note.
    r"|\b(?:кроме|за\s+исключением|однако)\b"
    # (7c) SUSPENDED / PARKED state: the thing exists but is not delivered
    # («Раздел 3 в морозилке», «Пункт 3 в подвешенном состоянии»).
    r"|\bв\s+(?:морозилк\w*|подвешенн\w*|ожидании|очеред\w*|статусе\s+черновик\w*)"
    # (8) emptiness predicated on the artifact
    r"|\bпуст(?:ой|ая|ое|ые|ым|ую|ого|ых)\b|\b0\s+(?:строк|тестов|записей|пунктов)"
    r"|\bни\s+одн\w+|\bне\s+единого"
    # (9) bare remainder participles
    r"|\b(?:пропущен|опущен|отложен|невыполнен)\w*\b"
    # (10) tooling markers and transliterated remainder words
    r"|\btodo\b|\bpending\b|\bstub\b|\bwip\b|\bfixme\b|\bremaining\b"
    r"|\bstill\s+open\b|\bnext\s+(?:step|steps|wave|phase)\b|\bmanual[_ ]review\b"
    r"|zaglush|\bnedodel|\bnedopis|\bnedosdan|\bnezakryt",
    re.IGNORECASE,
)

# «3 из 8» is partial credit; «30 из 30» / «4 из 4» is complete.
_N_OF_M_RE = re.compile(r"\b(\d+)\s+из\s+(\d+)\b")


def _partial_n_of_m(text: str) -> bool:
    return any(int(n) < int(m) for n, m in _N_OF_M_RE.findall(text))


_ATOM_STRIP = " \t.,:;!?…\"'«»-–—()[]{}"

# A Russian predicate: a finite verb, a short participle, or an infinitive. The
# past tense in -л is ambiguous with nouns («раздел 3», «узел»), so it is only
# accepted when the verb takes an object after it — a report's past-tense verb
# is followed by what it produced, a NOUN ending in -л heads its own phrase.
_PAST_VERB_RE = re.compile(r"\b([а-яё]{2,})(?:л|ла|ло|ли)\b", re.IGNORECASE)
_FINITE_PRED_RE = re.compile(
    r"\b[а-яё]+(?:ен|ён|ена|ено|ены|ан|ана|ано|аны|н|на|но|ны)\b"
    r"|\b[а-яё]+(?:ет|ёт|ит|ут|ют|ат|ят|ешь|ишь|ем|им|ете|ите)\b"
    r"|\b[а-яё]+(?:ть|ться|ти)\b"
    r"|\b[а-яё]+(?:ует|уют|ится|ятся|ется|ются|ался|ился|илась|ились)\b"
    r"|\b(?:passed|failed|done|complete|implemented|shipped|verified)\b",
    re.IGNORECASE,
)
# Short predicatives that assert a finished state without a finite verb.
_COMPLETION_ADJ_RE = re.compile(
    r"\b(?:готов\w*|зелён\w*|зелен\w*|собран\w*|написан\w*|проверен\w*"
    r"|выполнен\w*|закрыт\w*|завершён\w*|завершен\w*|сделан\w*|исправлен\w*"
    r"|реализован\w*|обновлён\w*|обновлен\w*|добавлен\w*|перенесён\w*"
    r"|перенесен\w*|установлен\w*|настроен\w*|подключён\w*|подключен\w*"
    r"|опубликован\w*|устранён\w*|устранен\w*|создан\w*|пройден\w*"
    r"|на\s+месте|в\s+порядке|работает|проходят|проходит|зелёные|зеленые"
    r"|passed|done|complete\w*|completed|implemented|shipped|landed"
    r"|finished|verified|resolved|deployed|merged|committed)\b",
    re.IGNORECASE,
)
# A MEASUREMENT survives without a predicate: a number with a unit, percent,
# currency, count or path. A bare digit next to a noun («раздел 3») is not a
# measurement — that is an enumerated remainder.
_MEASUREMENT_RE = re.compile(
    r"\d+\s*(?:%|шт|с\b|мс\b|сек\b|мин\b|ч\b|час\w*|кб|мб|гб|тб|байт\w*|б\b"
    r"|₽|\$|€|руб\w*|строк\w*|тестов\b|записей|пунктов|слайд\w*|файл\w*"
    r"|passed|failed|из\s+\d+)"
    r"|[A-Za-zА-Яа-я_\-]+\.(?:py|js|ts|md|json|html|zip|db|sql|yml|yaml|txt"
    r"|log|csv|svg|pdf|sh|toml|ini|cfg|xml)\b"
    r"|SHA-?256|HTTP\s*\d+|/",
    re.IGNORECASE,
)
# Bullet markers seen in worker notes. Em/en dashes are the most common.
_REMAINDER_LIST_RE = re.compile(r"^(?:[-*•+\u2014\u2013]|\d+[.)])\s+\S")
_BARE_LIST_MAX_WORDS = 14


def _has_predicate(atom: str) -> bool:
    """True when the atom ASSERTS something, not merely names a thing."""
    if _FINITE_PRED_RE.search(atom):
        return True
    for match in _PAST_VERB_RE.finditer(atom):
        rest = atom[match.end():].strip(" \t,.;:!?…)]»\"'")
        if not rest:
            continue
        if rest[0].isdigit() or rest[0] in ".!?;:,)]}":
            continue
        return True
    return False


def _iter_worker_atoms(residual: str):
    body = _worker_lines(residual)
    for sentence in _SENTENCE_SPLIT_RE.split(body):
        sentence = sentence.strip(_CLAUSE_EDGE_STRIP)
        if not sentence:
            continue
        for atom in _CLAUSE_LINK_SPLIT_RE.split(sentence):
            atom = atom.strip(_ATOM_STRIP)
            if atom:
                yield atom


def _is_bare_remainder_list(text: str) -> bool:
    """True when ``text`` is a short list of things with no predicate anywhere."""
    flat = " ".join(text.split()).strip(" \t,.;:!?…\"'«»-–—()[]{}")
    if not flat or len(flat.split()) > _BARE_LIST_MAX_WORDS:
        return False
    if (_has_predicate(flat) or _COMPLETION_ADJ_RE.search(flat)
            or _MEASUREMENT_RE.search(flat)):
        return False
    return True


def _has_remainder_list(note: str) -> bool:
    """True when the WORKER-WRITTEN body has a bullet/numbered line.

    Checked on the note's own lines, never on a span-stripped residual: removing
    the closer can leave a dangling «—» («Переделывать не нужно — артефакт
    готов»), which is punctuation, not a bullet. A worker-written bullet after
    the closer is the enumeration of what remains (r4 review, item 2).
    """
    for line in _worker_lines(note).splitlines():
        if _REMAINDER_LIST_RE.match(line.strip()):
            return True
    return False


_ABSENCE_RE = re.compile(
    r"\b([а-яё]{3,})\s+(?:нет|отсутствует|отсутствуют|не\s+существует)\b",
    re.IGNORECASE,
)


def _artifact_absence(atom: str) -> bool:
    """True when ``atom`` says something OTHER than a defect is missing.

    «замечаний нет, текста нет» — the defect is excused, but the artifact text
    is missing, which is open work. «замечаний нет» alone is not.
    """
    for m in _ABSENCE_RE.finditer(atom):
        if not re.search(_DEFECT_NOUN, m.group(1), re.IGNORECASE):
            return True
    return False


def _worker_words(text: str) -> list[str]:
    """Words that carry content (no punctuation-only tokens)."""
    return [w for w in " ".join(text.split()).split() if any(c.isalnum() for c in w)]


_CARD_CLOSING_ONLY = re.compile(
    # «…, кроме закрытия самой карточки» — the exclusion IS the closer, not
    # open work. Restricted to closing/card nouns so a real excluded artifact
    # («кроме интеграционных тестов») still reads as a remainder.
    r"(?:кроме|за\s+исключением|помимо)\s+(?:само[йго]\s+)?"
    r"(?:закрыти\w*|закрыть|перевод\w*|завершени\w*)\s+"
    r"(?:само[йго]\s+)?(?:карточк\w*|тикет\w*|задач\w*)",
    re.IGNORECASE,
)


_EXCLUSIVE_APPEND_RE = re.compile(
    r",\s*(?:кроме|за\s+исключением|однако|помимо\s+этого)\s+\S"
    # a sentence-initial contrast word needs no comma: «… Осталось закрыть
    # карточку. Но начала раздел 3.»
    r"|(?:^|[.!?;\n])\s*(?:но|однако|а)\s+[а-яё]{2,}",
    re.IGNORECASE,
)


def _residual_names_open_work(residual: str, strict: bool = True) -> bool:
    """Does ``residual`` name work that is still open?

    ``strict`` judges the worker's closing residual and treats a bare
    enumeration as remainder. The transcript tail is EARLIER narration («Начал
    работу.»), not the run's closing statement, so it may only veto on an
    explicit open-work signal — never on a bare noun. That asymmetry is the
    module's existing contract (see :func:`_split_note`) and it is what stops
    «Начал.» from vetoing a finished run.
    """
    # A comma + exclusive connective + content appends a second predication the
    # completion does not cover («Тесты зелёные, кроме интеграционных»). Judged
    # on the residual because the atom splitter separates the connective from
    # its object.
    if _EXCLUSIVE_APPEND_RE.search(residual) and not _CARD_CLOSING_ONLY.search(residual):
        return True
    for atom in _iter_worker_atoms(residual):
        if _NO_REDO_L1.search(atom) and not _artifact_absence(atom):
            continue
        if _partial_n_of_m(atom) or _OPEN_WORK_L2.search(atom):
            return True
    if strict:
        # A predicate-less atom names a THING, not a state of work — after the
        # closer that is a remainder («раздел 3», «тесты, схема»). Judged
        # per-atom ONLY when the residual is short: a long technical summary is
        # full of predicate-less fragments (headings, paths, English clauses)
        # that name nothing outstanding, and per-atom judging on the live board
        # dropped 467 finished runs to 13 accepted cards.
        atoms = list(_iter_worker_atoms(residual))
        short = sum(len(_worker_words(a)) for a in atoms) <= _BARE_LIST_MAX_WORDS
        if short:
            for atom in atoms:
                if _NO_REDO_L1.search(atom):
                    continue
                if not (_has_predicate(atom) or _COMPLETION_ADJ_RE.search(atom)
                        or _MEASUREMENT_RE.search(atom)):
                    return True
        for line in _worker_lines(residual).splitlines():
            if _is_bare_remainder_list(line):
                return True
    return False


def _find_all(haystack: str, needle: str):
    """Every start index of ``needle`` in ``haystack`` (both already lowered)."""
    start = haystack.find(needle)
    while start != -1:
        yield start
        start = haystack.find(needle, start + len(needle))


def _worker_residual(text: str, spans) -> str:
    """The note's own worker-written text with the closing statements removed.

    Everything the note says about the CARD being the only thing left — the
    matched closer clauses and the explicit "nothing left" phrases — is cut out,
    together with the collector's service lines and file inventory. What
    remains is the rest of what the worker wrote. If that residual still names
    open work, the card is not close-only, no matter where the remainder sits
    relative to the closer («Раздел 3 остаётся недоделанным. Осталось закрыть
    карточку.» names it BEFORE).
    """
    pieces: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if end <= cursor:
            continue
        if start > cursor:
            pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return _worker_lines("".join(pieces))


def _progress_reports_completion(progress: str) -> bool:
    """True only when the note says the work is done AND just closing remains.

    Two conditions must BOTH hold, and any negation/partial-credit qualifier
    vetoes first:

    1. the note names a *closing remainder* — «осталось (только) закрыть»
       ENDING its clause (optionally «карточку/тикет/задачу» as the last word)
       — or an explicit "nothing left to implement" statement;
    2. no negation («не полностью») and no partial-credit qualifier
       («частично», «на 70%», «кроме», «наполовину», «не начат») appears.

    Structural vetoes on top of the qualifier lists, because the remainder is
    often enumerated rather than qualified: a closer that does NOT end its
    clause («осталось закрыть: раздел 3, тесты» — `:`/`;` introduce the list,
    and a line break starts the same list on the next line), a note that
    re-opens the remainder in another clause («Осталось закрыть.\\\\nОсталось:
    раздел 3»), and a substantive tail after the closer that still names open
    work («Осталось закрыть карточку.» + «Код готов, тесты не писал.»).

    Deliberately narrow — it keys on the closing remainder, NOT on completion
    phrasing and NOT on the ``ВЫПОЛНЕНО РАНЕЕ`` header that
    :func:`agent.turn_finalizer._collect_budget_progress_note` always emits
    (that header means "this part already landed, continue from here", so
    matching it would collapse every real split into a single card).

    The asymmetry is intentional: a false positive drops the remaining work
    entirely (no card at all), a false negative costs one redundant split.
    """
    if not progress or not isinstance(progress, str):
        return False
    low = progress.lower()
    if any(neg in low for neg in _CLOSE_ONLY_NEGATIONS):
        return False
    # The blanket «any partial-credit word / any percentage» vetoes are gone.
    # On the live board «покрытие 91%» and «на 100%» are measurements inside a
    # FINISHED summary, and «закрыты, кроме закрытия самой карточки» is
    # close-only phrasing — both were over-split 100% of the time (review r7).
    # Partiality is now judged by PREDICATE in _OPEN_WORK_L2 («частично»,
    # «наполовину», «готовность 60%», «3 из 8», «не начат», «в работе»), and
    # subtraction is structural: _CLAUSE_LINK_SPLIT_RE splits on «кроме», so
    # «готово всё, кроме приложения Б» leaves «приложения Б» as its own atom,
    # which names work.

    # Every place the note states "the card is all that is left" — the closer
    # clauses and the explicit no-work-left phrases.
    spans: list[tuple[int, int]] = [
        (start, start + len(phrase))
        for phrase in _CLOSE_ONLY_NO_WORK_LEFT
        for start in _find_all(low, phrase)
    ]
    spans += [(m.start(), m.end()) for m in _CLOSE_REMAINDER_RE.finditer(low)]
    if not spans:
        return False

    if _CLOSE_REMAINDER_REOPENS_RE.search(low):
        return False
    transcript_tail = _split_note(low)[1]

    # (a) VETO layer, unchanged in spirit: anything that still names open work —
    # wherever the remainder sits relative to the closer — forces a real split.
    # The transcript tail is scanned here as veto-only evidence: on a real seam
    # the worker's own last narration may name a remainder the post-budget
    # summary skipped («Код готов, тесты не писал»). It is scanned with
    # strict=False: earlier narration must not veto on a bare noun.
    if _has_remainder_list(low):
        return False
    residual = _worker_residual(low, spans)
    if _residual_names_open_work(residual):
        return False
    if _residual_names_open_work(transcript_tail, strict=False):
        return False

    # (b) GRANT layer: the single card is allowed when nothing the worker wrote
    # names work that is still open. This is the DECISIVE test — it keys on
    # open work, not on whether each clause happens to be a recognised
    # completion wording. See _OPEN_WORK_L2 for the predicate families and the
    # measured cost of the whitelist this replaced.
    return True


@dataclass
class BudgetSplitResult:
    """Outcome of a budget-split attempt."""

    ok: bool
    child_ids: Optional[list[str]] = None
    reason: str = ""
    via: str = ""  # "llm" | "fallback" | "" (on failure)


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _extract_json_blob(raw: str) -> Optional[dict]:
    if not raw:
        return None
    import json
    import re

    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, Exception):
        return None
    return val if isinstance(val, dict) else None


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def budget_split_enabled(cfg: Optional[dict] = None) -> bool:
    """``kanban.budget_split`` — default True (the mechanism is opt-OUT)."""
    if not isinstance(cfg, dict):
        cfg = _load_config()
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    if isinstance(kanban_cfg, dict) and "budget_split" in kanban_cfg:
        return bool(kanban_cfg.get("budget_split"))
    return True


def _budget_split_max_depth(cfg: Optional[dict] = None) -> int:
    if not isinstance(cfg, dict):
        cfg = _load_config()
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    try:
        val = int(kanban_cfg.get("budget_split_max_depth", kb.DEFAULT_BUDGET_SPLIT_MAX_DEPTH))
    except (TypeError, ValueError):
        val = kb.DEFAULT_BUDGET_SPLIT_MAX_DEPTH
    return max(1, val)


def _close_only_child(task_id: str, title: str, body: str, progress: str) -> dict:
    """The single "verify and close" card for an already-finished split."""
    safe_title = _truncate(title or "task", 100)
    return {
        "title": f"[close only] Verify and close: {safe_title}",
        "body": (
            "Parent card: " + task_id + "\n"
            "Original title: " + safe_title + "\n\n"
            "Original body:\n" + (body or "(no body)")
            + "\n\nProgress note from the previous worker (budget ran out "
            "mid-work — continue from here, do NOT restart from scratch "
            "unless the state is broken):\n"
            + _truncate(progress, 2000)
            + "\n\nYOUR JOB: the work described above is ALREADY DONE — the "
            "artifact exists and was produced by the previous worker. DO NOT "
            "redo it, do not rewrite the artifact, do not start from scratch. "
            "Only verify the acceptance criteria from the original body "
            "against what already exists, then close the card with a short "
            "verification summary (what was checked, with what result). If "
            "you find a genuine defect, fix exactly that defect and nothing "
            "else."
        ),
        "parents": [],
    }


def _fallback_children(task_id: str, title: str, body: str, progress: str) -> list[dict]:
    """Deterministic split — no LLM required, always succeeds.

    Child 1 continues the remaining work (primed with the progress note);
    child 2 waits on child 1, then finishes/verifies and reports.

    Exception (class C): when the progress note reports the work as actually
    finished with only the card still open, resolve to ONE "verify and close"
    child instead of 2-4 — re-splitting finished work is the failure mode that
    produced 16 cards on t_2d037564.
    """
    if _progress_reports_completion(progress):
        return [_close_only_child(task_id, title, body, progress)]
    safe_title = _truncate(title or "task", 100)
    progress_block = ""
    if progress:
        progress_block = (
            "\n\nProgress note from the previous worker (budget ran out "
            "mid-work — continue from here, do NOT restart from scratch "
            "unless the state is broken):\n"
            + _truncate(progress, 2000)
        )
    base_body = (
        "Parent card: " + task_id + "\n"
        "Original title: " + safe_title + "\n\n"
        "Original body:\n" + (body or "(no body)")
        + progress_block
    )
    return [
        {
            "title": f"[split 1/2] Continue remaining work: {safe_title}",
            "body": (
                base_body
                + "\n\nYOUR JOB: inspect the current state of the work "
                "(files, artifacts, board comments of the parent card), "
                "complete the first half of the remaining work, and leave "
                "a clear handoff comment for part 2 (what is done, what "
                "remains). Verify what you produce; do not leave partial "
                "or unverified output."
            ),
            "parents": [],
        },
        {
            "title": f"[split 2/2] Finish and verify: {safe_title}",
            "body": (
                base_body
                + "\n\nYOUR JOB: part 1 (the parent split card "
                "'[split 1/2]') has already run. Read its comments and the "
                "current state, complete ALL remaining work of the "
                "original card, run the acceptance criteria from the "
                "original body, and finish with a short verification "
                "summary (what was checked, with what result)."
            ),
            "parents": [0],
        },
    ]


def _llm_propose_children(task, progress: str, timeout: Optional[int]) -> Optional[list[dict]]:
    """Ask the auxiliary splitter LLM for a 2-4 task graph. None on any failure."""
    try:
        from agent.auxiliary_client import call_llm  # type: ignore
    except Exception as exc:
        logger.debug("budget_split: auxiliary client import failed: %s", exc)
        return None

    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(task.body or "(no body)", 4000),
        progress=_truncate(progress or "(no progress note captured)", 3000),
    )
    try:
        resp = call_llm(
            task="kanban_budget_splitter",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=4000,
            timeout=timeout or 120,
        )
    except Exception as exc:
        logger.info("budget_split: API call failed for %s (%s)", task.id, exc)
        return None

    try:
        raw = resp.choices[0].message.content or ""
    except Exception:
        return None
    parsed = _extract_json_blob(raw)
    if parsed is None:
        return None

    raw_tasks = parsed.get("tasks")
    if not isinstance(raw_tasks, list) or not (2 <= len(raw_tasks) <= 4):
        return None
    children: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return None
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        body = entry.get("body")
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            parents = []
        clean_parents = [
            p for p in parents
            if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx
        ]
        children.append({
            "title": title.strip()[:200],
            "body": body if isinstance(body, str) else "",
            "parents": clean_parents,
        })
    return children


def _profile_author() -> str:
    import os

    return (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("USER")
        or "budget-splitter"
    )


# Sources of a recovered progress note, in priority order. Recorded on the
# ``budget_split`` event as ``progress_note_source`` so the "note went missing"
# class of failure is measurable instead of invisible.
PROGRESS_SOURCE_RUN_BUFFER = "run_buffer"
PROGRESS_SOURCE_PARENT_COMMENT = "parent_comment"
PROGRESS_SOURCE_WORKSPACE = "workspace"
PROGRESS_SOURCE_NONE = "none"

_RECOVERY_COMMENT_LIMIT = 1500
_RECOVERY_WORKSPACE_FILES = 20


def _recover_from_parent_comment(conn, task_id: str) -> str:
    """Last non-trivial comment on the card (the worker's own handoff notes)."""
    try:
        comments = kb.list_comments(conn, task_id) or []
    except Exception:
        return ""
    for comment in reversed(comments):
        body = getattr(comment, "body", "") or ""
        if len(body.strip()) >= 40:
            return body.strip()[-_RECOVERY_COMMENT_LIMIT:]
    return ""


def _recover_from_workspace(workspace_path: Optional[str]) -> str:
    """Recent change inventory of the card's workspace (``git status`` tail)."""
    if not workspace_path:
        return ""
    import os
    import subprocess

    if not os.path.isdir(workspace_path):
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace_path), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    lines = [ln.rstrip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    shown = lines[:_RECOVERY_WORKSPACE_FILES]
    return (
        "Рабочий каталог карточки содержит изменённые/новые файлы "
        f"({workspace_path}, git status --short, {len(lines)} записей):\n"
        + "\n".join(f"- {ln}" for ln in shown)
    )


def _resolve_progress_note(task, progress_note: str) -> tuple[str, str]:
    """Return ``(note, source)`` — the caller's note or a recovered one.

    Only runs when the caller passed an empty note (the turn_finalizer seam
    always passes one when the run left any trace). Recovery order: the card's
    last substantive comment, then the card workspace's change inventory. An
    exhausted card keeps splitting either way — the note is a handoff, not a
    gate — but the recorded source makes ``"none"`` countable.
    """
    note = (progress_note or "").strip()
    if note:
        return note, PROGRESS_SOURCE_RUN_BUFFER
    try:
        with kb.connect_closing() as conn:
            note = _recover_from_parent_comment(conn, task.id)
    except Exception:
        note = ""
    if note:
        return note, PROGRESS_SOURCE_PARENT_COMMENT
    note = _recover_from_workspace(getattr(task, "workspace_path", None))
    if note:
        return note, PROGRESS_SOURCE_WORKSPACE
    return "", PROGRESS_SOURCE_NONE


def run_budget_split(
    task_id: str,
    *,
    source: str,
    progress_note: str = "",
    expected_run_id: Optional[int] = None,
    cfg: Optional[dict] = None,
    llm_timeout: Optional[int] = 120,
) -> BudgetSplitResult:
    """Try to split ``task_id`` after operation-budget exhaustion.

    ``progress_note`` is the deterministic handoff captured by the caller
    (``agent.turn_finalizer``) from the exhausted run. When it is empty this
    function recovers one from the card's last comment or workspace before
    splitting, and records where the note came from.

    Returns ``BudgetSplitResult``. On ``ok=False`` the caller MUST fall
    back to its legacy terminal path (block / failure record) — a failed
    split must never strand a card.
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return BudgetSplitResult(False, reason="unknown task id")

    if cfg is None:
        cfg = _load_config()
    if not budget_split_enabled(cfg):
        return BudgetSplitResult(False, reason="budget_split disabled in config")

    note, note_source = _resolve_progress_note(task, progress_note)
    close_only = _progress_reports_completion(note)

    # LLM proposal first; deterministic fallback guarantees the split still
    # happens when the aux LLM is unavailable — the user asked for ALWAYS.
    # The close-only class skips the LLM entirely: there is nothing to
    # decompose, and asking would risk a 2-4 card fan-out over finished work.
    children = None
    via = "fallback"
    if not close_only:
        children = _llm_propose_children(task, note, llm_timeout)
        via = "llm"
    if not children:
        children = _fallback_children(
            task.id, task.title or "", task.body or "", note,
        )
        via = "fallback"
        if close_only:
            via = "close_only"
    # The LLM may return more than one card for an already-finished task; the
    # close-only contract wins regardless of which path produced the proposal.
    if _progress_reports_completion(note) and len(children) != 1:
        children = [_close_only_child(
            task.id, task.title or "", task.body or "", note,
        )]
        via = "close_only"

    try:
        with kb.connect_closing() as conn:
            child_ids = kb.split_task_on_budget(
                conn,
                task_id,
                children=children,
                author=_profile_author(),
                expected_run_id=expected_run_id,
                source=source,
                max_depth=_budget_split_max_depth(cfg),
                progress_note=note,
                progress_note_source=note_source,
                allow_single=len(children) == 1,
            )
    except Exception as exc:
        logger.exception("budget_split: DB error on task %s", task_id)
        return BudgetSplitResult(False, reason=f"DB error: {type(exc).__name__}")

    if not child_ids:
        return BudgetSplitResult(
            False,
            reason="split refused (not running / run mismatch / max depth)",
        )
    return BudgetSplitResult(
        True, child_ids=child_ids, via=via,
        reason=(
            f"split into {len(child_ids)} children via {via} "
            f"(progress source: {note_source}, {len(note)} chars)"
        ),
    )
