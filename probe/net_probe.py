"""Landlock network-restriction probe: does THIS kernel honour handled_access_net?

Facts go to JSON; every claim is a syscall return code, not a guess.
Run: python3 net_probe.py
"""
import ctypes, json, os, socket, struct, subprocess, sys, threading, time

libc = ctypes.CDLL("libc.so.6", use_errno=True)
SYS_create, SYS_add, SYS_restrict = 444, 445, 446
VERSION = 1
RULE_PATH_BENEATH, RULE_NET_PORT = 1, 2

FS = dict(EXECUTE=1 << 0, WRITE_FILE=1 << 1, READ_FILE=1 << 2, READ_DIR=1 << 3,
          REMOVE_FILE=1 << 4, REMOVE_DIR=1 << 5, MAKE_REG=1 << 6, MAKE_DIR=1 << 7,
          MAKE_SYM=1 << 8, MAKE_CHAR=1 << 9, MAKE_BLOCK=1 << 11, MAKE_SOCK=1 << 12,
          REFER=1 << 13, TRUNCATE=1 << 14, IOCTL_DEV=1 << 15)
NET = dict(BIND_TCP=1 << 0, CONNECT_TCP=1 << 1, BIND_UDP=1 << 2, CONNECT_SEND_UDP=1 << 3)

out = {}


class PB(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class Net(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


def abi():
    ctypes.set_errno(0)
    r = libc.syscall(SYS_create, None, 0, VERSION)
    return {"abi": r, "errno": ctypes.get_errno()}


def create_ruleset(fs_mask, net_mask):
    """landlock_create_ruleset(attr{handled_access_fs, handled_access_net}) -> fd or -errno."""
    attr = struct.pack("=Q", fs_mask) + struct.pack("=Q", net_mask)
    ctypes.set_errno(0)
    fd = libc.syscall(SYS_create, ctypes.create_string_buffer(attr, len(attr)), len(attr), 0)
    err = ctypes.get_errno()
    return fd, err


def add_path(fd, path, access):
    pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        r = libc.syscall(SYS_add, fd, RULE_PATH_BENEATH, ctypes.byref(PB(access, pfd)), 0)
        return r, ctypes.get_errno()
    finally:
        os.close(pfd)


def add_net(fd, port, access):
    ctypes.set_errno(0)
    r = libc.syscall(SYS_add, fd, RULE_NET_PORT, ctypes.byref(Net(access, port)), 0)
    return r, ctypes.get_errno()


def restrict(fd):
    ctypes.set_errno(0)
    p = libc.prctl(38, 1, 0, 0, 0)  # PR_SET_NO_NEW_PRIVS
    perr = ctypes.get_errno()
    ctypes.set_errno(0)
    r = libc.syscall(SYS_restrict, fd, 0)
    return {"nnp": p, "nnp_errno": perr, "restrict": r, "restrict_errno": ctypes.get_errno()}


def try_connect(host, port, timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return "CONNECTED"
    except PermissionError as e:
        return f"DENIED(errno={e.errno})"
    except ConnectionRefusedError:
        return "REFUSED"  # reached the network stack: the RULE did not block
    except Exception as e:
        return f"{type(e).__name__}:{e}"
    finally:
        s.close()


def child(mode):
    """Run the probing client in a fresh process so the parent keeps an unrestricted socket."""
    return subprocess.run([sys.executable, __file__, "--child", mode], capture_output=True, text=True, timeout=30, check=False)


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "--child":
    mode = sys.argv[2]
    if mode == "baseline":
        print(try_connect("127.0.0.1", 8731))
        print(try_connect("127.0.0.1", 8732))
        raise SystemExit
    fs_mask = sum(FS.values())
    net_mask = NET["CONNECT_TCP"] | NET["BIND_TCP"]
    fd, err = create_ruleset(fs_mask, net_mask)
    if fd < 0:
        print(f"RULESET_FAILED errno={err}")
        raise SystemExit(3)
    # one permitted read root + /dev,/tmp so the process can even start cleaning up
    for p, acc in (("/", FS["READ_FILE"] | FS["READ_DIR"] | FS["EXECUTE"]),):
        r, e = add_path(fd, p, acc)
        if r != 0:
            print(f"ADD_PATH_FAILED {p} errno={e}")
            raise SystemExit(4)
    r, e = add_net(fd, 8731, NET["CONNECT_TCP"])
    if r != 0:
        print(f"ADD_NET_FAILED errno={e}")
        raise SystemExit(5)
    res = restrict(fd)
    if res["restrict"] != 0:
        print(f"RESTRICT_FAILED {res}")
        raise SystemExit(6)
    print("ALLOWED_PORT", try_connect("127.0.0.1", 8731))
    print("DENIED_PORT", try_connect("127.0.0.1", 8732))
    raise SystemExit
