"""Probe 3: which (user_id, provider) shapes do LIVE sessions actually carry?"""
import os, sqlite3, glob
from gateway.owner_identity import principal_subject

dbs = ["/home/hermes/.hermes/state.db"] + glob.glob("/home/hermes/.hermes/profiles/*/state.db")
for db in dbs:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT user_id, provider, COUNT(*) FROM sessions WHERE user_id IS NOT NULL "
            "GROUP BY user_id, provider ORDER BY 3 DESC LIMIT 12").fetchall()
    except Exception as exc:
        print(db, "ERR", exc); continue
    print("\n==", db)
    for uid, prov, n in rows:
        subj = principal_subject({"user_id": uid, "provider": prov})
        print(f"  n={n:5d} user_id={uid!r:34s} provider={prov!r:12s} -> {subj}")
    con.close()
