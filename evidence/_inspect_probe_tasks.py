import sqlite3, sys  # probe-cleanup inspection
c = sqlite3.connect("/home/hermes/.hermes/kanban.db")
c.row_factory = sqlite3.Row
for r in c.execute("select id,title,status,workspace_kind,workspace_path,created_at from tasks where title like 'probe %'"):
    print(dict(r))
