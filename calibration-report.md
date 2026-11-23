# Замер калибровки оси минут (t_5e861e7e)

Источник: `/home/hermes/.hermes/kanban.db`, таблицы `task_runs` JOIN `tasks`.
Длительность = `ended_at − started_at`, секунды → минуты, прогоны с обеими метками.
Пункты приёмки считаются тем же кодом, что и в продакшене:
`agent.kanban_card_sizing.count_acceptance_items` по телу задачи.

Метод перцентилей — **nearest rank**: `sorted(v)[ceil(p/100 × n) − 1]`.
Это тот же метод, которым снята таблица в теле карточки (4 пункта → p80 79 мин,
0 пунктов → 61 мин, общий p80 76 мин).

## Скрипт

```python
import math, sqlite3
from agent.kanban_card_sizing import count_acceptance_items

SQL = """SELECT tr.started_at, tr.ended_at, t.body FROM task_runs tr
          JOIN tasks t ON t.id = tr.task_id
         WHERE tr.started_at IS NOT NULL AND tr.ended_at IS NOT NULL"""

def nearest_rank(values, p):
    v = sorted(values)
    return v[max(1, math.ceil(p / 100.0 * len(v))) - 1]

buckets = {}
for started, ended, body in sqlite3.connect('/home/hermes/.hermes/kanban.db').execute(SQL):
    minutes = (ended - started) / 60.0
    if minutes <= 0:
        continue
    buckets.setdefault(count_acceptance_items(body), []).append(minutes)

for items in sorted(buckets):
    v = buckets[items]
    print(f"{items}: n={len(v)} p50={nearest_rank(v,50):.1f} "
          f"p80={nearest_rank(v,80):.1f} >60={100*sum(x>60 for x in v)/len(v):.0f}%")
```

## Вывод (23.11.2026, 763 прогона с пригодной длительностью)

```
0: n=223 p50=23.0 p80=60.7 >60=21%
2: n=4   p50=40.5 p80=71.7 >60=25%
3: n=52  p50=39.6 p80=81.2 >60=29%
4: n=233 p50=42.1 p80=78.9 >60=30%
5: n=170 p50=45.5 p80=90.3 >60=36%
6: n=54  p50=51.6 p80=91.4 >60=48%
7: n=9   p50=47.8 p80=118.2 >60=44%
8: n=18  p50=21.5 p80=72.0 >60=22%
```

Вся выборка: n=763, медиана 38.1 мин, p80 76.4 мин, 30 % дольше 60 мин,
40 % дольше 50 мин.

## Вошедшие в модуль точки

`CALIBRATION_P50_MINUTES = {0: 23, 3: 40, 4: 42, 5: 46, 6: 52}`
`CALIBRATION_P80_MINUTES = {0: 61, 3: 81, 4: 79, 5: 90, 6: 91}`

Отброшены намеренно:

- **2 пункта** — всего 4 прогона; попадает в линейную интерполяцию 61→81.
- **7 и более** — 27 прогонов суммарно, и обе группы меряют *ниже* шестипунктовой
  (7: p80 118 при n=9 — шум; 8+: p80 72 при n=18). Это в основном карточки-родители
  fan-out, чьё время в основном оркестрация, а не работа. Протаскивать этот провал
  значило бы предсказывать крупной карточке меньше времени, чем мелкой, поэтому выше
  шести пунктов значение удерживается на точке 6.

## Расхождение с первым снимком

Первая версия констант снималась со снимка n=754 методом линейной интерполяции и
давала `{3: 81, 5: 90, 6: 88, 7: 96}`. Повторный замер на n=763 тем же методом, что и
тело карточки, дал `{3: 81, 5: 90, 6: 91}` и снял точку 7. Числа в модуле — вторые.
