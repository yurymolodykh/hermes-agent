
import sqlite3
from agent.kanban_card_sizing import count_acceptance_items

SQL = """
SELECT tr.started_at, tr.ended_at, t.body
  FROM task_runs tr JOIN tasks t ON t.id = tr.task_id
 WHERE tr.started_at IS NOT NULL AND tr.ended_at IS NOT NULL
"""

def pct(values, p):
    v = sorted(values)
    k = (len(v) - 1) * p / 100.0
    lo = int(k); hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)

c = sqlite3.connect('/home/hermes/.hermes/kanban.db')
buckets = {}
for s, e, body in c.execute(SQL):
    d = (e - s) / 60.0
    if d <= 0:
        continue
    buckets.setdefault(count_acceptance_items(body), []).append(d)

total = sum(len(v) for v in buckets.values())
allv = [x for v in buckets.values() for x in v]
print("usable runs:", total, "median:", round(pct(allv,50),1),
      ">60:", round(100*sum(x>60 for x in allv)/len(allv)), "%",
      ">50:", round(100*sum(x>50 for x in allv)/len(allv)), "%")
print(f"{'items':>5} {'n':>4} {'p50':>7} {'p80':>7} {'>60%':>5}")
for k in sorted(buckets):
    v = buckets[k]
    print(f"{k:>5} {len(v):>4} {pct(v,50):>7.1f} {pct(v,80):>7.1f} {100*sum(x>60 for x in v)/len(v):>4.0f}%")
