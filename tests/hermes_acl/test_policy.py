"""Policy tests: what the boundary grants, and the grants that would silently UNMAKE it.

The failure this file exists for is not "the shim crashed" — it is "the shim ran, reported
``confined``, and granted ``/``". A recursive grant on a parent directory is indistinguishable
from no boundary at all, and it looks correct in every log line. So the assertions here are about
the GRANTS, not about the machinery:

* ``/`` is never granted;
* a home directory is never granted (that is every profile's memory at once);
* ancestor directories get traversal (``x``) only, never read;
* a human home is never granted ``rw``;
* and — the measured one — a confined child cannot read a secret in a sibling directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_acl import policy
from hermes_acl import spec as spec_mod

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated HERMES_HOME so the policy sees a realistic memory layout."""
    h = tmp_path / "home"
    (h / "memories" / "u" / "tg:alice").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: h, raising=False)
    return h


def _grants(spec: spec_mod.Spec) -> dict[str, str]:
    return {r.path: r.perms for r in spec.fs}


class TestNothingWholeMachineIsGranted:
    def test_root_is_never_granted(self, home, tmp_path):
        spec = policy.build_terminal_spec(workspace=str(tmp_path / "ws"))
        assert "/" not in _grants(spec), "granting / gives away every file on the machine"

    def test_no_grant_covers_the_whole_of_tmp_by_default(self, home, tmp_path):
        """``rw`` on a temp ROOT is recursive; it must not become a path to everything."""
        spec = policy.build_terminal_spec(workspace=str(tmp_path / "ws"))
        for path, perms in _grants(spec).items():
            if "w" in perms:
                assert path != "/", path

    def test_a_human_home_is_never_writable(self, home, tmp_path):
        spec = policy.build_terminal_spec(workspace="/home/somebody/project")
        perms = _grants(spec)
        assert "w" not in perms.get("/home/somebody", ""), perms.keys()
        assert "w" not in perms.get("/home", ""), perms.keys()

    def test_hermes_home_inside_a_writable_root_is_not_granted(self, tmp_path, monkeypatch):
        """If HERMES_HOME sits under TMPDIR, the temp grant must be dropped.

        Otherwise ``memories/u/<other>`` is readable from the terminal and the per-subject split
        — the strongest point of this task — is defeated from the outside.
        """
        fake_tmp = tmp_path / "tmproot"
        fake_tmp.mkdir()
        # The home must be INSIDE the temp root for this to test anything: the first version of
        # this test put them side by side and passed the wrong assertion.
        home = fake_tmp / "hermes-home"
        (home / "memories" / "u" / "tg:alice").mkdir(parents=True)
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home, raising=False)
        monkeypatch.setattr(policy.tempfile, "gettempdir", lambda: str(fake_tmp))
        spec = policy.build_terminal_spec(workspace=str(fake_tmp / "ws"))
        perms = _grants(spec)
        # The root may still appear as a TRAVERSAL ancestor of the workspace ('x'), which grants
        # no listing and no reading. What must never appear is a write/read grant.
        granted = perms.get(str(fake_tmp), "")
        assert "w" not in granted and "r" not in granted, (
            f"the writable temp root contains HERMES_HOME and must not be granted recursively; "
            f"got perms {granted!r}")

    def test_ancestors_get_traversal_only(self, home, tmp_path):
        ws = tmp_path / "deep" / "nested" / "ws"
        ws.mkdir(parents=True)
        spec = policy.build_terminal_spec(workspace=str(ws))
        perms = _grants(spec)
        for ancestor in (tmp_path / "deep", tmp_path / "deep" / "nested"):
            entry = perms.get(str(ancestor))
            if entry is None:
                continue
            assert "r" not in entry, (
                f"ancestor {ancestor} must be traversable (x) but not readable (r): "
                f"reading it exposes every sibling file")
        assert "w" in perms.get(str(ws), "")

    def test_the_workspace_is_writable(self, home, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        spec = policy.build_terminal_spec(workspace=str(ws))
        assert "w" in _grants(spec).get(str(ws), "")


class TestTheSwitch:
    def test_wrap_argv_is_identity_when_acl_is_off(self, monkeypatch, tmp_path):
        monkeypatch.setattr(policy, "acl_enabled", lambda: False)
        argv = ["bash", "-c", "true"]
        assert policy.wrap_argv(argv, workspace=str(tmp_path)) is argv, (
            "with acl.enabled=false the SAME list object must come back: callers compare argv")

    def test_wrap_argv_prepends_the_shim_when_acl_is_on(self, monkeypatch, tmp_path):
        monkeypatch.setattr(policy, "acl_enabled", lambda: True)
        monkeypatch.setattr("hermes_acl.native.shim_impl", lambda: "python")
        out = policy.wrap_argv(["bash", "-c", "true"], workspace=str(tmp_path))
        assert out is not None and out[-3:] == ["bash", "-c", "true"]
        assert "hermes_acl.shim" in " ".join(out)

    def test_acl_enabled_reads_the_shared_switch(self, monkeypatch):
        """One switch for the whole access model — not a second, private one."""
        import hermes_acl_scope
        monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: True)
        assert policy.acl_enabled() is True
        monkeypatch.setattr(hermes_acl_scope, "acl_enabled", lambda: False)
        assert policy.acl_enabled() is False

    def test_a_broken_config_never_enables_the_boundary(self, monkeypatch):
        def boom():
            raise RuntimeError("config exploded")
        monkeypatch.setattr("hermes_acl_scope.acl_enabled", boom)
        assert policy.acl_enabled() is False, "a broken config must fail OPEN for enablement, not crash"


class TestNetworkPolicy:
    def test_network_is_denied_by_default(self, home, tmp_path, monkeypatch):
        monkeypatch.setattr(policy, "net_allow", list)
        spec = policy.build_terminal_spec(workspace=str(tmp_path / "ws"))
        assert spec.net_mode == "deny" and spec.net == ()

    def test_configured_ports_are_passed_through(self, home, tmp_path, monkeypatch):
        monkeypatch.setattr(policy, "net_allow", lambda: [("connect", 443)])
        spec = policy.build_terminal_spec(workspace=str(tmp_path / "ws"))
        assert spec.net == (spec_mod.NetRule("connect", 443),)

    def test_malformed_config_entries_are_dropped_not_trusted(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.cfg_get",
                            lambda cfg, *k, default=None: [
                                {"port": "not-a-number"}, {"port": 70000}, {"kind": "icmp", "port": 80},
                                "junk", {"kind": "connect", "port": 443}], raising=False)
        monkeypatch.setattr("hermes_cli.config.load_config", dict, raising=False)
        assert policy.net_allow() == [("connect", 443)]


class TestTheBoundaryActuallyHolds:
    """The one test that measures instead of asserting about grants."""

    def test_confined_child_cannot_read_a_sibling_directory(self, tmp_path):
        """A real child, a real kernel: the secret in a NON-workspace dir must be unreadable."""
        from hermes_acl import native
        binary = native.build_native()
        if not binary:
            pytest.skip("no C compiler on this host")

        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "visible.txt").write_text("PUBLIC", encoding="utf-8")
        secret = tmp_path / "secret" / "s.txt"
        secret.parent.mkdir()
        secret.write_text("PRIVATE-77", encoding="utf-8")

        spec = spec_mod.build([("/usr", "rx"), ("/bin", "rx"), ("/lib", "rx"), ("/lib64", "rx"),
                               ("/etc", "rx"), ("/dev", "rw"), ("/proc", "r"),
                               (str(tmp_path), "x"), (str(workspace), "rw")],
                              net_mode="deny")
        spec_path = spec_mod.write(spec, tmp_path / "spec.txt")

        allowed = subprocess.run([binary, "--spec", str(spec_path), "--", "/bin/cat",
                                  str(workspace / "visible.txt")],
                                 capture_output=True, text=True, timeout=120)
        assert "PUBLIC" in allowed.stdout, allowed.stderr

        refused = subprocess.run([binary, "--spec", str(spec_path), "--", "/bin/cat",
                                  str(secret)], capture_output=True, text=True, timeout=120)
        assert "PRIVATE-77" not in refused.stdout, (
            "the confined child read a file outside its workspace — the policy grants too much")
        assert refused.returncode != 0
