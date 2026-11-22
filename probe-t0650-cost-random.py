"""Стоимость tar на плохо сжимаемых данных (худший случай для gzip)."""
import os, pathlib, tempfile, time

from agent.kanban_budget_checkpoint import _archive_tree, _changed_paths

for n_files, kb in ((100, 100), (1000, 100), (2000, 500)):
    home = pathlib.Path(tempfile.mkdtemp(prefix="probe-t0650-rand-"))
    os.environ["HERMES_KANBAN_HOME"] = str(home)
    tree = home / "tree"
    tree.mkdir()
    for i in range(n_files):
        (tree / f"f{i}.bin").write_bytes(os.urandom(kb * 1024))
    files = _changed_paths(tree)
    total = sum((tree / f).stat().st_size for f in files)
    t0 = time.perf_counter()
    archive = _archive_tree(tree, files, f"t_rand_{n_files}_{kb}", "probe")
    dt = time.perf_counter() - t0
    size_mb = pathlib.Path(archive).stat().st_size / 1e6 if archive else 0.0
    print(f"файлов={len(files)} несжимаемых={total / 1e6:.0f} МБ "
          f"-> архив={size_mb:.0f} МБ за {dt * 1000:.0f} мс")
