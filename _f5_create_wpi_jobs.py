"""F5: create wpi-collect (no_agent) and wpi-synthesize (agent) cron jobs
in the 01-orchestrator cron store via the canonical cron.jobs.create_job API.
Runs with HERMES_HOME=<01-orchestrator profile> so the store resolves correctly.
"""
import os
import sys

HERMES_HOME = r"C:/Users/hermes/AppData/Local/hermes/profiles/01-orchestrator"
WORKSPACE = r"C:/Users/hermes/AppData/Local/hermes/profiles/01-orchestrator/durable/rgz/weekly-platform-intel"
DELIVER_PAIR = "api_server:owui-f76faefd-be76-4781-93f7-b3f047cda4bb,telegram:125805788"

os.environ["HERMES_HOME"] = HERMES_HOME

from cron.jobs import create_job, get_job, list_jobs  # noqa: E402

# ---- 1) wpi-collect (no_agent, deterministic collector) ----
collect_prompt = (
    "wpi-collect (ГЗ weekly-platform-intel, Ф5): детерминированный сбор "
    "площадок (read-only GET, python stdlib), запись только в data/ mission-workspace."
)
collect = create_job(
    prompt=None,
    schedule="5 1 * * 6",
    name="wpi-collect",
    script="collector.py",
    no_agent=True,
    deliver="local",
    workdir=WORKSPACE,
)
collect_id = collect["id"]
print("wpi-collect id:", collect_id)

# ---- 2) wpi-synthesize (agent, LLM synthesis + delivery) ----
synth_prompt = (
    "Самодостаточный бриф LLM-звена синтеза weekly-platform-intel (1 итерация/неделю). "
    "Прочитай файл C:/Users/hermes/AppData/Local/hermes/profiles/01-orchestrator/durable/rgz/weekly-platform-intel/synth/prompt.md "
    "и ВЫПОЛНИ его как полную инструкцию: сгенерируй 2 отчёта "
    "(reports/YYYY-W##-A.md / reports/YYYY-W##-B.md + HTML, стиль Up Great), доставь их "
    "владельцу MEDIA-вложениями + компактным digest в чат-пару "
    + DELIVER_PAIR +
    ". ЖЁСТКИЕ ИНВАРИАНТЫ (бриф уточняет): модель/провайдер не менять "
    "(deepseek-v4-flash/custom:letovo); прод НЕ менять (никаких правок навыков/SOUL/памяти/"
    "профилей/MCP/настроек без явного решения владельца); вход только локальный (workspace "
    "миссии) + web_extract по списку из брифа (кап 5); секреты/тела сообщений/базы не читать "
    "и не выгружать; токен-кап входа <=16000; при сбое доставки зафиксировать "
    "last_delivery_error и пути файлов. Не задавай вопросов (headless)."
)
synth = create_job(
    prompt=synth_prompt,
    schedule="45 1 * * 6",
    name="wpi-synthesize",
    context_from=[collect_id],
    skills=[],
    enabled_toolsets=["web", "terminal", "file", "skills"],
    workdir=WORKSPACE,
    deliver=DELIVER_PAIR,
    model="deepseek-v4-flash",
    provider="custom:letovo",
)
synth_id = synth["id"]
print("wpi-synthesize id:", synth_id)

# verify
for jid in (collect_id, synth_id):
    j = get_job(jid)
    print("----", jid, j["name"])
    print("  schedule:", j["schedule"].get("display") if isinstance(j.get("schedule"), dict) else j.get("schedule"))
    print("  no_agent:", j.get("no_agent"), "| script:", j.get("script"))
    print("  workdir:", j.get("workdir"))
    print("  context_from:", j.get("context_from"))
    print("  enabled_toolsets:", j.get("enabled_toolsets"))
    print("  deliver:", j.get("deliver"))
    print("  model/provider:", j.get("model"), "/", j.get("provider"))
print("TOTAL_ACTIVE:", sum(1 for jj in list_jobs() if jj.get("enabled", True)))
