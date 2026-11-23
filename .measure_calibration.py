
import sqlite3, math
from agent.kanban_card_sizing import count_acceptance_items

SQL = """SELECT tr.started_at, tr.ended_at, t.body FROM task_runs tr
          JOIN tasks t ON t.id = tr.task_id
         WHERE tr.started_at IS NOT NULL AND tr.ended_at IS NOT NULL"""

def nearest_rank(values, p):
    v = sorted(values)
    if not v: return None
    rank = max(1, math.ceil(p / 100.0 * len(v)))
    return v[rank - 1]

c = sqlite3.connect('/home/hermes/.hermes/kanban.db')
b = {}
for s, e, body in c.execute(SQL):
    d = (e - s) / 60.0
    if d <= 0: continue
    b.setdefault(count_acceptance_items(body), []).append(d)

tot = sum(len(v) for v in b.values())
allv = sorted(x for v in b.values() for x in v)
print("runs:", tot, "median:", round(nearest_rank(allv,50),1),
      "p80:", round(nearest_rank(allv,80),1),
      ">60:", round(100*sum(x>60 for x in allv)/len(allv)), "%",
      ">50:", round(100*sum(x>50 for x in allv)/len(allv)), "%")
print(f"{'items':>5} {'n':>4} {'p50':>7} {'p80':>7} {'>60%':>5}")
for k in sorted(b):
    v = b[k]
    print(f"{k:>5} {len(v):>4} {nearest_rank(v,50):>7.1f} {nearest_rank(v,80):>7.1f} {100*sum(x>60 for x in v)/len(v):>4.0f}%")

ge7 = [x for k, v in b.items() if k >= 7 for x in v]
print(f"7+    {len(ge7):>4} {nearest_rank(ge7,50):>7.1f} {nearest_rank(ge7,80):>7.1f} {100*sum(x>60 for x in ge7)/len(ge7):>4.0f}%")
