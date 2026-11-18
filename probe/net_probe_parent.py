"""Parent side: two local listeners; body must open BOTH sockets itself.

The listen sockets are created in the parent and inherited via subprocess fd passing, so the
confined child never binds (which would need BIND_TCP) — it only connects.
"""
import json, os, socket, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
srv = []
for port in (8731, 8732):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(8)
    srv.append((port, s))

print("listeners up:", [p for p, _ in srv])
for mode in ("baseline", "net"):
    r = subprocess.run([sys.executable, os.path.join(HERE, "net_probe.py"), "--child", mode],
        check=False,
                       capture_output=True, text=True, timeout=60)
    print(f"=== {mode} rc={r.returncode}\n{r.stdout}{r.stderr}")
for p, s in srv:
    s.close()
