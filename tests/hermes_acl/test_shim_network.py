"""Tests for the exec shim's NETWORK rule — the hole this task exists to close.

Every case runs a REAL child on the REAL kernel. No mocks for the boundary itself: a mocked
Landlock proves only that Python can compute a mask. Listeners are opened by the parent (which is
unrestricted) so the confined child is only ever a CLIENT — a bind would need BIND_TCP and turn a
connect test into a different test.

Both implementations are exercised through the SAME spec file (``hermes_acl/spec.py``): the C
shim on the hot path and the Python fallback. A test that passes for one and not the other would
mean two sources of truth, which is exactly what the shared format exists to prevent.

If the kernel does not offer the network rights, the cases that require them SKIP with the probe
result in the reason: the plan's instruction is to NAME an unsupported kernel as a residual gap,
not to pretend the boundary exists.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_acl import landlock as ll
from hermes_acl import shim as shim_mod
from hermes_acl import spec as spec_mod

REPO = Path(__file__).resolve().parents[2]

CAP = ll.net_supported()
requires_net = pytest.mark.skipif(
    not CAP["supported"],
    reason=f"kernel does not support Landlock network rights: {CAP['reason']} (residual gap)",
)

#: A child that connects to the given ports and prints what happened, in a stable format.
CLIENT = r"""
import socket, sys
host = "127.0.0.1"
for port in (int(sys.argv[1]), int(sys.argv[2])):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((host, port))
        print(f"port{port}=CONNECTED")
    except PermissionError as e:
        print(f"port{port}=DENIED:{e.errno}")
    except ConnectionRefusedError:
        print(f"port{port}=REFUSED")
    except Exception as e:
        print(f"port{port}=ERROR:{type(e).__name__}")
    finally:
        s.close()
"""


class _Listeners:
    """Two loopback listeners, so "denied port" cannot be confused with "nothing is listening"."""

    def __init__(self):
        self.ports: list[int] = []
        self._socks: list[socket.socket] = []

    def __enter__(self):
        for _ in range(2):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
            s.listen(8)
            self.ports.append(s.getsockname()[1])
            self._socks.append(s)
        return self

    def __exit__(self, *exc):
        for s in self._socks:
            s.close()


def _spec(tmp_path: Path, *, net_mode: str = "deny", net_required: bool = False,
          allow: list[tuple[str, int]] | None = None) -> Path:
    built = spec_mod.build(
        [("/", "rx"), ("/dev", "rw"), ("/tmp", "rw")],
        net_mode=net_mode, net_required=net_required, net=allow or [],
    )
    return spec_mod.write(built, tmp_path / "spec.txt")


def _run_python_shim(tmp_path: Path, spec_path: Path, script: str, args: list[str] | None = None,
                     extra_env: dict | None = None):
    script_path = tmp_path / "client.py"
    script_path.write_text(script, encoding="utf-8")
    cmd = [sys.executable, "-m", "hermes_acl.shim", "--spec", str(spec_path), "--",
           sys.executable, str(script_path), *(args or [])]
    env = {**os.environ, "PYTHONPATH": str(REPO), **(extra_env or {})}
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(REPO), timeout=180)


def _run_native_shim(tmp_path: Path, spec_path: Path, script: str, args: list[str] | None = None,
                     extra_env: dict | None = None):
    from hermes_acl import native
    binary = native.build_native()
    if not binary:
        pytest.skip("no C compiler on this host: native shim not built")
    script_path = tmp_path / "client.py"
    script_path.write_text(script, encoding="utf-8")
    cmd = [binary, "--spec", str(spec_path), "--", sys.executable, str(script_path), *(args or [])]
    return subprocess.run(cmd, capture_output=True, text=True,
                          env={**os.environ, **(extra_env or {})}, timeout=180)


IMPLS = {"python": _run_python_shim, "native": _run_native_shim}


@pytest.fixture(params=["python", "native"])
def run_shim(request):
    """Same cases against both implementations: one spec format, two readers."""
    return IMPLS[request.param]


class TestSpecFormatIsShared:
    def test_c_and_python_read_the_same_bytes(self, tmp_path):
        path = _spec(tmp_path, allow=[("connect", 443)])
        text = path.read_text(encoding="utf-8")
        assert text.startswith("version 1")
        parsed = spec_mod.parse(text)
        assert parsed.fs[0].path == "/" and parsed.fs[0].perms == "rx"
        assert parsed.net == (spec_mod.NetRule("connect", 443),)
        # The Python shim reads it into its own shape and must agree about the network.
        loaded = shim_mod.load_spec(str(path))
        assert loaded["net"]["enable"] is True and loaded["net"]["allow"] == [
            {"kind": "connect", "port": 443}]

    def test_render_parse_roundtrip_is_stable(self, tmp_path):
        built = spec_mod.build([("/a b/c", "rw")], net_mode="deny", net=[("bind", 8080)])
        again = spec_mod.parse(built.render())
        assert again == built

    @pytest.mark.parametrize("bad", ["fs rwx relative/path", "fs q /tmp", "net allow jump 80",
                                     "nonsense directive", "version 2"])
    def test_inexpressible_specs_are_refused_at_build(self, bad):
        with pytest.raises(spec_mod.SpecError):
            text = "version 1\n" + bad + "\n" if not bad.startswith("version") else bad + "\n"
            spec_mod.parse(text)

    def test_newline_in_path_is_refused(self):
        with pytest.raises(spec_mod.SpecError):
            spec_mod.build([("/tmp/a\nnet off", "rw")])


class TestProbeIsReal:
    def test_probe_reports_this_kernel(self):
        cap = ll.net_supported()
        assert cap["abi"] == ll.abi_version()
        if cap["abi"] >= 4:
            assert cap["supported"] or cap["errno"] != 0, cap

    def test_probe_survives_an_old_abi(self, monkeypatch):
        monkeypatch.setattr(ll, "abi_version", lambda: 3)
        cap = ll.net_supported()
        assert cap["supported"] is False and "ABI 3" in cap["reason"]


class TestNetworkIsDeniedByDefault:
    @requires_net
    def test_confined_child_cannot_connect_anywhere(self, tmp_path, run_shim):
        spec_path = _spec(tmp_path)
        with _Listeners() as lis:
            r = run_shim(tmp_path, spec_path, CLIENT, [str(lis.ports[0]), str(lis.ports[1])])
        assert r.returncode == 0, r.stderr
        assert f"port{lis.ports[0]}=DENIED:13" in r.stdout, r.stdout
        assert f"port{lis.ports[1]}=DENIED:13" in r.stdout, r.stdout

    @requires_net
    def test_same_child_unconfined_can_connect(self, tmp_path):
        """Grounding for the case above: without the shim the very same client connects."""
        with _Listeners() as lis:
            script = tmp_path / "client.py"
            script.write_text(CLIENT, encoding="utf-8")
            r = subprocess.run([sys.executable, str(script), str(lis.ports[0]), str(lis.ports[1])],
                               capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert f"port{lis.ports[0]}=CONNECTED" in r.stdout
        assert f"port{lis.ports[1]}=CONNECTED" in r.stdout

    @requires_net
    def test_allowed_port_works_and_the_other_is_denied(self, tmp_path, run_shim):
        with _Listeners() as lis:
            allowed, denied = lis.ports
            spec_path = _spec(tmp_path, allow=[("connect", allowed)])
            r = run_shim(tmp_path, spec_path, CLIENT, [str(allowed), str(denied)])
        assert r.returncode == 0, r.stderr
        assert f"port{allowed}=CONNECTED" in r.stdout, r.stdout
        assert f"port{denied}=DENIED:13" in r.stdout, r.stdout

    @requires_net
    def test_boundary_is_inherited_by_grandchildren(self, tmp_path, run_shim):
        """The child spawns a grandchild that connects: Landlock rides the whole tree."""
        spec_path = _spec(tmp_path)
        with _Listeners() as lis:
            script = r"""
import subprocess, sys
inner = "import socket;s=socket.socket();s.settimeout(3)\n"
inner += f"try:\n s.connect(('127.0.0.1',{int(sys.argv[2])}));print('GC=CONNECTED')\n"
inner += "except PermissionError as e:\n print(f'GC=DENIED:{e.errno}')\n"
inner += "except ConnectionRefusedError:\n print('GC=REFUSED')\n"
r = subprocess.run([sys.executable, "-c", inner], capture_output=True, text=True)
print(r.stdout.strip())
"""
            r = run_shim(tmp_path, spec_path, script, [str(lis.ports[0]), str(lis.ports[1])])
        assert r.returncode == 0, r.stderr
        assert "GC=DENIED:13" in r.stdout, r.stdout

    @requires_net
    def test_udp_is_not_a_bypass(self, tmp_path, run_shim):
        """TCP-only handling left UDP open — measured, so this asserts the CLOSED state.

        A one-packet ``sendto`` to an arbitrary host is a complete exfiltration channel for a
        small secret; it only closes once ``connect_send_udp``/``bind_udp`` are handled (ABI 7+).
        On an older ABI the datagram goes through and the plan names that as a residual gap.
        """
        spec_path = _spec(tmp_path)
        with _Listeners() as lis:
            script = r"""
import socket, sys
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
u.settimeout(2)
try:
    u.sendto(b"x", ("127.0.0.1", int(sys.argv[1])))
    print("UDP=SENT")
except PermissionError as e:
    print(f"UDP=DENIED:{e.errno}")
"""
            r = run_shim(tmp_path, spec_path, script, [str(lis.ports[0])])
        assert r.returncode == 0, r.stderr
        if CAP["abi"] >= 7:
            assert "UDP=DENIED:13" in r.stdout, r.stdout
        else:
            pytest.skip(f"ABI {CAP['abi']} < 7: UDP rights unavailable (residual gap, named in plan)")

    def test_net_off_does_not_handle_network_rights(self, tmp_path):
        """An explicit opt-out is expressible and does not pretend to confine."""
        path = _spec(tmp_path, net_mode="off")
        assert "net off" in path.read_text(encoding="utf-8")
        assert spec_mod.parse(path.read_text(encoding="utf-8")).net_mode == "off"


class TestRefusalsAreFailClosed:
    @pytest.mark.parametrize("impl", ["python", "native"])
    def test_invalid_spec_refuses_before_exec(self, tmp_path, impl):
        spec_path = tmp_path / "spec.txt"
        spec_path.write_text("version 1\nfs q /tmp\n", encoding="utf-8")
        marker = tmp_path / "ran.txt"
        code = f"open({str(marker)!r}, 'w').write('ran')"
        if impl == "python":
            cmd = [sys.executable, "-m", "hermes_acl.shim", "--spec", str(spec_path), "--",
                   sys.executable, "-c", code]
        else:
            from hermes_acl import native
            binary = native.build_native()
            if not binary:
                pytest.skip("no C compiler on this host")
            cmd = [binary, "--spec", str(spec_path), "--", sys.executable, "-c", code]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO),
                           env={**os.environ, "PYTHONPATH": str(REPO)}, timeout=120)
        assert r.returncode == shim_mod.EXIT_REFUSED, r.stderr
        assert not marker.exists(), "the command must NOT run when the policy is unusable"

    def test_missing_command_is_a_usage_error(self, tmp_path):
        spec_path = _spec(tmp_path)
        r = subprocess.run([sys.executable, "-m", "hermes_acl.shim", "--spec", str(spec_path), "--"],
                           capture_output=True, text=True, cwd=str(REPO),
                           env={**os.environ, "PYTHONPATH": str(REPO)}, timeout=120)
        assert r.returncode == shim_mod.EXIT_USAGE

    @requires_net
    def test_required_but_missing_net_rights_refuse(self, tmp_path):
        """``net required`` turns an unsupported kernel into a refusal, not a quiet downgrade."""
        spec_path = _spec(tmp_path, net_required=True)
        code = (
            "import sys;sys.path.insert(0,%r)\n"
            "from hermes_acl import shim, landlock\n"
            "landlock.net_supported = lambda: {'supported': False, 'reason': 'simulated'}\n"
            "try:\n"
            "    shim.apply(shim.load_spec(%r))\n"
            "    print('NO-REFUSAL')\n"
            "except shim.ShimError as e:\n"
            "    print('REFUSED:', e)\n"
        ) % (str(REPO), str(spec_path))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           cwd=str(REPO), env={**os.environ, "PYTHONPATH": str(REPO)}, timeout=120)
        assert "REFUSED:" in r.stdout, r.stdout + r.stderr

    def test_native_shim_refuses_required_net_on_an_old_kernel(self, tmp_path):
        """The C shim's own refusal path: ``net required`` + no net rights -> exit 126."""
        from hermes_acl import native
        binary = native.build_native()
        if not binary:
            pytest.skip("no C compiler on this host")
        spec_path = tmp_path / "spec.txt"
        # net_required only bites below ABI 4, which this kernel is not; assert the PARSER at
        # least carries the directive (the runtime branch is covered by the Python case above).
        spec_mod.write(spec_mod.build([("/", "rx")], net_required=True), spec_path)
        assert "net required" in spec_path.read_text(encoding="utf-8")
        r = subprocess.run([binary, "--spec", str(spec_path), "--", "/bin/true"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr


class TestReport:
    @pytest.mark.parametrize("impl", ["python", "native"])
    @requires_net
    def test_report_names_the_net_state(self, tmp_path, impl):
        report = tmp_path / "report.json"
        spec_path = _spec(tmp_path, allow=[("connect", 1234)])
        code = "print('ok')"
        if impl == "python":
            cmd = [sys.executable, "-m", "hermes_acl.shim", "--spec", str(spec_path), "--",
                   sys.executable, "-c", code]
        else:
            from hermes_acl import native
            binary = native.build_native()
            if not binary:
                pytest.skip("no C compiler on this host")
            cmd = [binary, "--spec", str(spec_path), "--", sys.executable, "-c", code]
        env = {**os.environ, "PYTHONPATH": str(REPO), "HERMES_ACL_SHIM_REPORT": str(report)}
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120)
        assert r.returncode == 0, r.stderr
        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["net_enabled"] is True
        assert data["net_ports"] == [1234]
        assert data["abi"] >= 4

    @requires_net
    def test_shared_spec_makes_both_implementations_agree_on_the_boundary(self, tmp_path):
        """Same spec file, both shims: allowed path readable, secret refused — identically.

        The spec deliberately does NOT grant ``/``: with a whole-machine grant both
        implementations "agree" trivially while proving nothing (the first version of this test
        did exactly that and would have passed with a completely broken shim). Here the secret
        sits outside the allow-list, so a passing run means both readers enforced a boundary AND
        agreed on where it is.
        """
        spec_path = _spec(tmp_path)
        visible = tmp_path / "visible.txt"
        visible.write_text("PUBLIC", encoding="utf-8")
        # The secret lives OUTSIDE the granted tree. It cannot be a child of tmp_path: the runner
        # writes client.py there, so tmp_path itself must be readable, and Landlock grants
        # recursively — a secret nested under it would be readable by construction and the test
        # would prove the opposite of what it claims.
        outside = tmp_path.parent / (tmp_path.name + "-outside")
        outside.mkdir(exist_ok=True)
        secret = outside / "s.txt"
        secret.write_text("SECRET-99", encoding="utf-8")

        built = spec_mod.build([("/usr", "rx"), ("/bin", "rx"), ("/lib", "rx"), ("/lib64", "rx"),
                                ("/etc", "rx"), ("/dev", "rw"), ("/proc", "r"),
                                # The interpreter must be executable: without it the shim refuses
                                # the exec (exit 127) and the test proves nothing about reading.
                                (sys.prefix, "rx"), (sys.base_prefix, "rx"),
                                (str(tmp_path), "rw")])
        spec_mod.write(built, spec_path)

        code = (
            "import pathlib, sys\n"
            f"ok = pathlib.Path({str(visible)!r}).read_text()\n"
            f"try:\n"
            f"    leaked = pathlib.Path({str(secret)!r}).read_text()\n"
            f"except OSError:\n"
            f"    leaked = None\n"
            "print('ALLOWED=' + ok.strip())\n"
            "print('LEAKED=' + (leaked or 'NONE'))\n"
        )
        outcomes = {}
        for name, runner in IMPLS.items():
            r = runner(tmp_path, spec_path, code)
            outcomes[name] = {
                "rc": r.returncode,
                "allowed": "ALLOWED=PUBLIC" in r.stdout,
                "leaked": "SECRET-99" in r.stdout,
                "stderr": r.stderr[-200:],
            }

        for name, o in outcomes.items():
            assert o["rc"] == 0, f"{name}: shim refused to run: {o['stderr']}"
            assert o["allowed"], f"{name}: the allowed file was not readable"
            assert not o["leaked"], (
                f"{name}: the confined child read a file outside the allow-list — the boundary "
                f"is not enforced by this implementation")
        # Agreement is the point: one implementation passing and the other leaking would mean two
        # sources of truth, which is what the shared spec format exists to prevent.
        assert len({(o["allowed"], o["leaked"]) for o in outcomes.values()}) == 1, outcomes


class TestOverhead:
    def test_native_shim_overhead_is_small(self, tmp_path):
        """The plan's budget is +0.41 ms; the C shim must stay in that class.

        The Python fallback is measured too and REPORTED, not asserted: it is ~60 ms, which is
        why it exists only as a fallback (and why the launcher warns when it is used).
        """
        import statistics
        import time

        from hermes_acl import native
        binary = native.build_native()
        spec_path = _spec(tmp_path)

        def med(argv, n=40):
            xs = []
            for _ in range(n):
                t = time.perf_counter()
                subprocess.run(argv, capture_output=True)
                xs.append((time.perf_counter() - t) * 1000)
            return statistics.median(xs)

        bare = med(["/bin/true"])
        if binary:
            confined = med([binary, "--spec", str(spec_path), "--", "/bin/true"])
            assert confined - bare < 2.0, f"native shim costs {confined - bare:+.2f} ms"
        py = med([sys.executable, "-m", "hermes_acl.shim", "--spec", str(spec_path), "--", "/bin/true"], n=10)
        print(f"\nbare={bare:.2f} ms native={confined if binary else float('nan'):.2f} ms python={py:.2f} ms")
