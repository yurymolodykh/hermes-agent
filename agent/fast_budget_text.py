"""Fast-lane budget overflow notice text (fast-lane part 3, «не молчать»).

Owner decision 2026-10-19 (task t_a7e7d08a): when a fast request (profile 11)
does not fit into the ~5-minute soft budget, the user must be told — with a
short status and actionable options — instead of the run silently continuing
until an answer finally arrives.

The message below is the single source of truth for the user-visible text and
is emitted through two channels (see conversation_loop._fire_fast_budget_user_notice):

  * live: ``agent._fast_budget_notice_callback`` -> OWUI transient status pill;
  * durable: appended as a footer to the final assistant response so the
    options survive in the saved answer even if the transient pill was missed.

Requirement format-minimum:
  "Задача не уложилась в быстрый режим (~5 мин). [краткий статус выполненного].
   Варианты: (1) продолжить как есть — ответ придет по завершении;
   (2) перевести в карточку — напишите «в карточку»;
   (3) запустить режим «подумай» — напишите «подумай» с тем же вопросом."

A generic "работа продолжается" is used here (the model-specific progress
detail is already being streamed as tool progress / content; the status pill
must stay short and not duplicate the answer).
"""

FAST_BUDGET_USER_NOTICE = (
    "Задача не уложилась в быстрый режим (~5 мин). "
    "Работа продолжается — ответ придёт по завершении. "
    "Варианты: (1) продолжить как есть — просто дождитесь ответа; "
    "(2) перевести в карточку — напишите «в карточку»; "
    "(3) запустить режим «подумай» — напишите «подумай» с тем же вопросом."
)
