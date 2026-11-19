
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    s = socket.create_connection((host, port), timeout=3)
    s.close()
    print("CONNECTED")
except PermissionError as e:
    print(f"DENIED(errno={e.errno})")
except ConnectionRefusedError:
    print("REFUSED")
except Exception as e:
    print(f"{type(e).__name__}:{e}")
