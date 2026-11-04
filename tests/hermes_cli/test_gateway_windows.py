"""Tests for hermes_cli.gateway_windows."""

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.gateway as gateway
import hermes_cli.gateway_windows as gateway_windows
import hermes_cli.setup as setup


_BREAKAWAY_MARKER = "_HERMES_GATEWAY_BREAKAWAY"




def test_schtasks_encoding_falls_back_to_utf8(monkeypatch):
    """A broken/empty locale must not leave us without a decoder (issue #38172)."""

    monkeypatch.setattr(gateway_windows.locale, "getpreferredencoding", lambda *a, **k: "")
    assert gateway_windows._schtasks_encoding() == "utf-8"

    def _boom(*args, **kwargs):
        raise RuntimeError("locale exploded")

    monkeypatch.setattr(gateway_windows.locale, "getpreferredencoding", _boom)
    assert gateway_windows._schtasks_encoding() == "utf-8"




@pytest.mark.windows_only
def test_build_gateway_argv_keeps_venv_console_python_for_uv_venv(monkeypatch, tmp_path):
    """No pythonw / base-interpreter detour: the venv console python.exe is
    launched hidden (CREATE_NO_WINDOW) so descendants inherit its hidden
    console instead of flashing their own (#54220/#56747).

    Windows-only: ``_build_gateway_argv()`` asserts the host is Windows and the
    argv/env overlay it returns is built from real Windows path separators and
    ``Scripts/python.exe`` layout — a patched ``sys.platform`` covered the
    branch but not any of that.
    """

    project = tmp_path / "project"
    scripts = project / "venv" / "Scripts"
    site_packages = project / "venv" / "Lib" / "site-packages"
    hermes_home = tmp_path / "hermes-home"
    base = tmp_path / "uv" / "python" / "cpython-3.11-windows-x86_64-none"
    scripts.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    hermes_home.mkdir()
    base.mkdir(parents=True)

    venv_python = scripts / "python.exe"
    venv_pythonw = scripts / "pythonw.exe"
    base_pythonw = base / "pythonw.exe"
    for exe in (venv_python, venv_pythonw, base_pythonw):
        exe.write_text("", encoding="utf-8")
    (project / "venv" / "pyvenv.cfg").write_text(
        f"home = {base}\nimplementation = CPython\nuv = 0.11.14\nversion_info = 3.11.15\n",
        encoding="utf-8",
    )

    import hermes_cli.gateway as gateway

    monkeypatch.setattr(gateway, "PROJECT_ROOT", project)
    monkeypatch.setattr(gateway, "get_python_path", lambda: str(venv_python))
    monkeypatch.setattr(gateway, "_profile_arg", lambda hermes_home: "")
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: str(hermes_home))

    argv, cwd, env_overlay = gateway_windows._build_gateway_argv()

    assert argv[:3] == [str(venv_python), "-m", "hermes_cli.main"]
    assert cwd == str(hermes_home.resolve())
    assert env_overlay["VIRTUAL_ENV"] == str(project / "venv")
    assert str(project) in env_overlay["PYTHONPATH"].split(gateway_windows.os.pathsep)


@pytest.mark.windows_only
def test_spawn_detached_marks_primary_breakaway_success(monkeypatch, tmp_path, caplog):
    """A successful breakaway spawn reports true without a warning."""
    argv = ["python.exe", "-m", "hermes_cli.main", "gateway", "run"]
    cwd = str(tmp_path)
    calls = []

    def fake_popen(call_argv, **kwargs):
        calls.append((call_argv, kwargs))
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(
        gateway_windows,
        "_build_gateway_argv",
        lambda: (argv, cwd, {"HERMES_GATEWAY_DETACHED": "1"}),
    )
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gateway_windows.subprocess, "Popen", fake_popen)
    caplog.set_level(logging.WARNING, logger=gateway_windows.__name__)

    assert gateway_windows._spawn_detached() == 12345
    assert len(calls) == 1
    actual_argv, kwargs = calls[0]
    assert actual_argv == argv
    assert kwargs["cwd"] == cwd
    assert kwargs["creationflags"] == gateway_windows.windows_detach_flags()
    assert kwargs["env"][_BREAKAWAY_MARKER] == "1"
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is kwargs["stderr"]
    assert not caplog.records


@pytest.mark.windows_only
def test_spawn_detached_warns_and_marks_no_breakaway_fallback(
    monkeypatch, tmp_path, caplog
):
    """A denied breakaway retries once with private false metadata."""
    argv = ["python.exe", "-m", "hermes_cli.main", "gateway", "run"]
    cwd = str(tmp_path)
    calls = []

    def fake_popen(call_argv, **kwargs):
        calls.append((call_argv, kwargs))
        if len(calls) == 1:
            error = OSError(13, "Access is denied")
            error.winerror = 5
            raise error
        return SimpleNamespace(pid=23456)

    monkeypatch.setattr(
        gateway_windows,
        "_build_gateway_argv",
        lambda: (
            argv,
            cwd,
            {"HERMES_GATEWAY_DETACHED": "1", "SECRET_SENTINEL": "do-not-log"},
        ),
    )
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gateway_windows.subprocess, "Popen", fake_popen)
    caplog.set_level(logging.WARNING, logger=gateway_windows.__name__)

    assert gateway_windows._spawn_detached() == 23456
    assert len(calls) == 2
    (argv_primary, primary), (argv_fallback, fallback) = calls
    assert argv_primary == argv_fallback == argv
    assert primary["cwd"] == fallback["cwd"] == cwd
    assert primary["creationflags"] == gateway_windows.windows_detach_flags()
    assert (
        fallback["creationflags"]
        == gateway_windows.windows_detach_flags_without_breakaway()
    )
    assert primary["stdin"] is fallback["stdin"] is subprocess.DEVNULL
    assert primary["stdout"] is primary["stderr"]
    assert fallback["stdout"] is fallback["stderr"]
    assert Path(primary["stdout"].name) == Path(fallback["stdout"].name)
    assert primary["close_fds"] is fallback["close_fds"] is True
    assert primary["env"] is not fallback["env"]
    assert primary["env"][_BREAKAWAY_MARKER] == "1"
    assert fallback["env"][_BREAKAWAY_MARKER] == "0"
    assert {
        key: value for key, value in primary["env"].items() if key != _BREAKAWAY_MARKER
    } == {
        key: value for key, value in fallback["env"].items() if key != _BREAKAWAY_MARKER
    }

    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "5" in warnings[0].getMessage()
    assert "do-not-log" not in warnings[0].getMessage()
    assert str(tmp_path) not in warnings[0].getMessage()


class TestStableWindowsGatewayWorkingDir:
    def test_stable_gateway_working_dir_uses_hermes_home(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: home)
        assert gateway_windows._stable_gateway_working_dir(tmp_path / "checkout") == str(home.resolve())

    def test_stable_gateway_working_dir_falls_back_to_project_root(self, tmp_path, monkeypatch):
        missing = tmp_path / "missing" / ".hermes"
        project = tmp_path / "checkout"
        monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: missing)
        assert gateway_windows._stable_gateway_working_dir(project) == str(project)




def _arrange_startup_fallback(monkeypatch, tmp_path, running_pids):
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
    startup_entry = tmp_path / "Startup" / "Hermes_Gateway_alice.cmd"
    calls = []

    monkeypatch.setattr(gateway_windows, "_prompt_install_choices", lambda *args, **kwargs: (False, True))
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway_alice")
    monkeypatch.setattr(gateway_windows, "_write_task_script", lambda: script_path)
    monkeypatch.setattr(
        gateway_windows,
        "_install_scheduled_task",
        lambda task_name, script_path: (
            False,
            "schtasks /Create failed (code 1): ERROR: Access is denied.",
        ),
    )
    monkeypatch.setattr(gateway_windows, "_should_fall_back", lambda code, detail: True)
    monkeypatch.setattr(gateway_windows, "_is_running_as_admin", lambda: True)
    monkeypatch.setattr(
        gateway_windows,
        "_launch_elevated_install",
        lambda force=False, start_now=None, start_on_login=None: calls.append(("elevate", force, start_now, start_on_login)) or True,
    )

    def fake_install_startup_entry(path: Path) -> Path:
        calls.append(("install_startup", path))
        return startup_entry

    monkeypatch.setattr(gateway_windows, "_install_startup_entry", fake_install_startup_entry)
    monkeypatch.setattr(gateway_windows, "_spawn_detached", lambda path: calls.append(("spawn", path)) or 12345)
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: calls.append(("report_start", via)))
    monkeypatch.setattr(gateway_windows, "_print_next_steps", lambda: calls.append(("next_steps", None)))
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda: running_pids)
    monkeypatch.setattr(gateway, "_profile_arg", lambda: "--profile alice")
    return script_path, calls




@pytest.mark.windows_only
def test_elevated_gateway_command_uses_hidden_console_python(monkeypatch):
    """UAC handoff launches console python with SW_HIDE — a single hidden
    console, not console-less pythonw (#54220/#56747), and no visible
    elevated cmd.exe window left open.

    Windows-only: the code path runs behind ``_assert_windows()`` and goes
    through ``ctypes.windll.shell32``, neither of which exists on a faked
    host. ShellExecuteW itself stays mocked — it would raise a real UAC
    prompt — but the host identity is genuine.
    """
    calls = []

    class FakeShell32:
        def ShellExecuteW(self, hwnd, verb, executable, params, cwd, show):
            calls.append((hwnd, verb, executable, params, cwd, show))
            return 33

    class FakeWindll:
        shell32 = FakeShell32()

    monkeypatch.setattr(gateway_windows, "_current_profile_cli_args", lambda: ["--profile", "alice"])
    monkeypatch.setattr(gateway_windows.sys, "executable", r"C:\Hermes\venv\Scripts\python.exe")
    monkeypatch.setattr(gateway_windows.ctypes, "windll", FakeWindll(), raising=False)

    assert gateway_windows._launch_elevated_gateway_command("install", ["--start-now", "--elevated-handoff"])

    assert len(calls) == 1
    _hwnd, verb, executable, params, cwd, show = calls[0]
    assert verb == "runas"
    assert executable == r"C:\Hermes\venv\Scripts\python.exe"
    assert "--profile alice gateway install --start-now --elevated-handoff" in params
    assert show == 0
    assert cwd


def test_install_scheduled_task_recreates_instead_of_change(monkeypatch, tmp_path):
    """Install must delete+create so stale minute-repeat task settings are not preserved.

    Host-agnostic on purpose: ``_install_scheduled_task`` only renders the task
    XML and shells out through ``_exec_schtasks`` (mocked here as the genuine
    external dependency), so no platform fake is needed.
    """
    calls = []
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
    xml_seen = {}

    monkeypatch.setattr(gateway_windows, "_resolve_task_user", lambda: r"DOMAIN\\alice")

    def fake_schtasks(args):
        calls.append(tuple(args))
        if args[0] == "/Delete":
            return (0, "SUCCESS", "")
        if args[0] == "/Create":
            xml_path = Path(args[args.index("/XML") + 1])
            xml_seen["text"] = xml_path.read_text(encoding="utf-16")
            return (0, "SUCCESS", "")
        raise AssertionError(f"unexpected schtasks args: {args}")

    monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake_schtasks)
    ok, detail = gateway_windows._install_scheduled_task("Hermes_Gateway_alice", script_path)

    assert ok is True
    assert "/Change" not in [arg for call in calls for arg in call]
    assert calls[0][:4] == ("/Delete", "/F", "/TN", "Hermes_Gateway_alice")
    assert calls[1][0] == "/Create"
    assert "/XML" in calls[1]
    assert "/SC" not in calls[1]
    assert "<Delay>PT30S</Delay>" in xml_seen["text"]
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml_seen["text"]
    assert "<StopOnIdleEnd>false</StopOnIdleEnd>" in xml_seen["text"]
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml_seen["text"]
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml_seen["text"]
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml_seen["text"]
    assert "<RestartOnFailure>" in xml_seen["text"]
    assert "<Count>999</Count>" in xml_seen["text"]
    # Scheduled Task launches the console-less .vbs via wscript.exe, never cmd.exe
    # (issue #45599 fix A: no console -> no logon CTRL_CLOSE_EVENT / 0xC000013A).
    assert "<Command>wscript.exe</Command>" in xml_seen["text"]
    assert "//B //Nologo" in xml_seen["text"]
    assert "Hermes_Gateway_alice.vbs" in xml_seen["text"]
    assert "cmd.exe" not in xml_seen["text"]


def test_gateway_vbs_script_is_console_less(monkeypatch):
    """The .vbs launcher must avoid cmd.exe entirely and Run pythonw hidden
    (issue #45599 fix A: no console -> no logon CTRL_CLOSE_EVENT / 0xC000013A)."""
    monkeypatch.setattr(
        gateway_windows,
        "_resolve_detached_python",
        lambda exe: (r"C:\venv\Scripts\pythonw.exe", Path(r"C:\venv"), []),
    )
    content = gateway_windows._build_gateway_vbs_script(
        r"C:\venv\Scripts\python.exe",
        r"C:\Hermes",
        r"C:\Hermes",
        "--profile work",
    )
    assert "cmd.exe" not in content.lower()
    assert 'CreateObject("WScript.Shell")' in content
    assert "pythonw.exe" in content
    assert "hermes_cli.main" in content
    assert "gateway run" in content
    assert ", 0, False" in content  # hidden window, detached/async
    for var in ("HERMES_HOME", "PYTHONIOENCODING", "HERMES_GATEWAY_DETACHED", "VIRTUAL_ENV", "PYTHONPATH"):
        assert var in content
    assert "--profile" in content and "work" in content
    assert content.endswith("\r\n")














# ---------------------------------------------------------------------------
# Inherited kanban budget arms must not reach a freshly spawned gateway.
#
# t_ff216dbf. ``_spawn_detached`` builds the child env as
# ``{**os.environ, **env_overlay}`` and ``env_overlay`` carries neither
# ``HERMES_KANBAN_MAX_RUNTIME_SECONDS`` nor ``HERMES_KANBAN_RUN_STARTED_AT``.
# So when an agent starts a new gateway process from inside a kanban worker,
# the fresh gateway adopts that worker's cap and start time as its OWN
# os.environ. The dispatcher then builds every child env from
# ``dict(os.environ)``, so a card with ``max_runtime_seconds = NULL`` would be
# spawned with a foreign clock, armed at step 1 and force-closed with no work
# done — the F1 defect (t_4a7fae37) re-created one layer above the code fix.
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
def test_spawn_detached_purges_inherited_kanban_budget_arms(monkeypatch, tmp_path):
    """A gateway process spawned from a kanban worker must not inherit its budget."""
    argv = ["python.exe", "-m", "hermes_cli.main", "gateway", "run"]
    captured = {}

    def fake_popen(call_argv, **kwargs):
        captured["env"] = kwargs["env"]
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(
        gateway_windows,
        "_build_gateway_argv",
        lambda: (argv, str(tmp_path), {"HERMES_GATEWAY_DETACHED": "1"}),
    )
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gateway_windows.subprocess, "Popen", fake_popen)
    # The worker session this launch happens inside of.
    monkeypatch.setenv("HERMES_KANBAN_MAX_RUNTIME_SECONDS", "10800")
    monkeypatch.setenv("HERMES_KANBAN_RUN_STARTED_AT", "1793681473")

    gateway_windows._spawn_detached()

    env = captured["env"]
    assert "HERMES_KANBAN_MAX_RUNTIME_SECONDS" not in env, (
        "inherited runtime cap leaked into the new gateway env"
    )
    assert "HERMES_KANBAN_RUN_STARTED_AT" not in env, (
        "inherited run start-time leaked into the new gateway env"
    )
    # The launcher's own pin must survive the purge.
    assert env["HERMES_GATEWAY_DETACHED"] == "1"


# ---------------------------------------------------------------------------
# `gateway start` must never install a login auto-start behind the caller's
# back (t_db577abc).
#
# Observed 2026-11-03: an agent session ran `... gateway start` with no TTY.
# gateway_windows.start() fell through to install(), install() asked three
# yes/no questions, prompt_yes_no() returned its `True` default for each
# because there was nobody to answer, and a Startup-folder login item appeared
# on the host. install(start_now=True) then spawned a gateway, and start()
# spawned a second one on top of it.
#
# Contract now: start() refuses in an unattended context, never installs
# persistence without --install-service, and never double-spawns.
# ---------------------------------------------------------------------------


def _arrange_no_service(monkeypatch):
    """No service registered, no gateway running, nothing spawned."""
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: False)
    monkeypatch.setattr(gateway_windows, "is_startup_entry_installed", lambda: False)


def test_prompt_install_choices_fails_closed_when_unattended(monkeypatch):
    """No TTY + no explicit answer must raise, not accept the True default."""
    monkeypatch.setattr(setup, "is_noninteractive", lambda: True)
    monkeypatch.delenv("HERMES_GATEWAY_INSTALL_START_NOW", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_INSTALL_START_ON_LOGIN", raising=False)

    with pytest.raises(gateway_windows.NonInteractiveInstallError) as exc:
        gateway_windows._prompt_install_choices()
    assert "--start-on-login" in str(exc.value)

    # An explicit answer is accepted without a terminal.
    assert gateway_windows._prompt_install_choices(True, False) == (True, False)


def test_prompt_install_choices_honours_env_answers(monkeypatch):
    """Unattended callers can answer via env; both keys are then complete."""
    monkeypatch.setattr(setup, "is_noninteractive", lambda: True)
    monkeypatch.setenv("HERMES_GATEWAY_INSTALL_START_NOW", "1")
    monkeypatch.setenv("HERMES_GATEWAY_INSTALL_START_ON_LOGIN", "0")

    assert gateway_windows._prompt_install_choices() == (True, False)


def test_start_refuses_unattended_without_service(monkeypatch, capsys):
    """The reported incident: unattended `start` must not install anything."""
    _arrange_no_service(monkeypatch)
    monkeypatch.setattr(setup, "is_noninteractive", lambda: True)
    installs = []
    monkeypatch.setattr(
        gateway_windows, "install", lambda **kw: installs.append(kw)
    )

    with pytest.raises(gateway_windows.GatewayServiceNotInstalledError) as exc:
        gateway_windows.start()

    assert "gateway install" in str(exc.value)
    assert installs == [], "start() installed a service without being asked"
    assert "Gateway service is not installed" in capsys.readouterr().out


def test_start_install_service_still_fails_closed_without_login_choice(monkeypatch):
    """--install-service implies start-now, not an unanswered auto-start."""
    _arrange_no_service(monkeypatch)
    monkeypatch.setattr(setup, "is_noninteractive", lambda: True)
    monkeypatch.delenv("HERMES_GATEWAY_INSTALL_START_ON_LOGIN", raising=False)
    created = []
    monkeypatch.setattr(
        gateway_windows, "install", lambda **kw: (_ for _ in ()).throw(
            gateway_windows.NonInteractiveInstallError("no answer for --start-on-login")
        )
    )
    monkeypatch.setattr(
        gateway_windows, "_install_startup_entry", lambda p: created.append(p)
    )

    with pytest.raises(gateway_windows.GatewayServiceNotInstalledError):
        gateway_windows.start(install_service=True)

    assert created == [], "a Startup login item was created without an explicit answer"


def test_start_interactive_prompt_defaults_to_no(monkeypatch):
    """A human pressing Enter must not install a login item either."""
    _arrange_no_service(monkeypatch)
    monkeypatch.setattr(setup, "is_noninteractive", lambda: False)
    asked = {}

    def fake_prompt(question, default=True):
        asked["default"] = default
        return default

    monkeypatch.setattr(setup, "prompt_yes_no", fake_prompt)
    installs = []
    monkeypatch.setattr(gateway_windows, "install", lambda **kw: installs.append(kw))

    gateway_windows.start()

    assert asked["default"] is False, "Enter must not mean 'install an auto-start'"
    assert installs == []


def test_start_does_not_double_spawn_after_install(monkeypatch):
    """install(start_now=True) captures the only spawn; start() must see it."""
    _arrange_no_service(monkeypatch)
    monkeypatch.setattr(setup, "is_noninteractive", lambda: True)
    monkeypatch.setenv("HERMES_GATEWAY_INSTALL_START_ON_LOGIN", "1")

    state = {"installed": False}
    pids = []
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: list(pids))
    monkeypatch.setattr(
        gateway_windows, "is_startup_entry_installed", lambda: state["installed"]
    )
    spawned = []

    def fake_install(**kwargs):
        # install(start_now=True) registers the login item AND launches the
        # gateway itself.
        spawned.append("install")
        state["installed"] = True
        pids.append(4242)

    monkeypatch.setattr(gateway_windows, "install", fake_install)
    monkeypatch.setattr(
        gateway_windows, "_spawn_detached", lambda *a, **kw: spawned.append("start") or 111
    )
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: None)

    gateway_windows.start(install_service=True)

    assert spawned == ["install"], f"expected a single spawn, got {spawned}"


@pytest.mark.windows_only
def test_restart_respawn_never_installs_persistence(monkeypatch):
    """restart() respawns a process; it must not evaluate install questions."""
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "stop", lambda: None)
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_absent", lambda **kw: True)
    monkeypatch.setattr(gateway_windows.time, "sleep", lambda s: None)
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", lambda **kw: [999])
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: False)
    monkeypatch.setattr(gateway_windows, "is_startup_entry_installed", lambda: False)
    installs = []
    monkeypatch.setattr(gateway_windows, "install", lambda **kw: installs.append(kw))
    monkeypatch.setattr(gateway_windows, "_spawn_detached", lambda *a, **kw: 999)
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: None)

    gateway_windows.restart()

    assert installs == []


# ---------------------------------------------------------------------------
# An operator launcher that owns the lifecycle (service_supervisor) must still
# be able to start a gateway with no login item registered.
#
# fast_answers_gateway_start.cmd / safe_gateway_start.cmd call `gateway start`
# from a supervisor tick. Those launchers are the persistence mechanism, so the
# absence of a Startup entry is expected — refusing there would turn a healthy
# recovery path into a permanent DOWN. They opt out with an env flag because
# the decision has to cross the CLI boundary.
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
def test_start_honours_supervisor_allow_uninstalled_env(monkeypatch):
    """HERMES_GATEWAY_ALLOW_UNINSTALLED_START=1 keeps supervised starts working."""
    _arrange_no_service(monkeypatch)
    monkeypatch.setattr(setup, "is_noninteractive", lambda: True)
    monkeypatch.setenv("HERMES_GATEWAY_ALLOW_UNINSTALLED_START", "1")
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])
    installs: list = []
    monkeypatch.setattr(
        gateway_windows, "install", lambda **kw: installs.append(kw) or "startup-fallback"
    )
    spawned: list = []
    monkeypatch.setattr(
        gateway_windows, "_spawn_detached", lambda *a, **kw: spawned.append(1) or 777
    )
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: None)

    gateway_windows.start()

    assert spawned == [1], "supervised start must launch exactly one gateway"
    assert installs == [], "a supervised start must not install a login item"


@pytest.mark.windows_only
def test_start_flag_is_off_by_default(monkeypatch):
    """No env flag, no kwarg -> unattended start still refuses (fail closed)."""
    _arrange_no_service(monkeypatch)
    monkeypatch.setattr(setup, "is_noninteractive", lambda: True)
    monkeypatch.delenv("HERMES_GATEWAY_ALLOW_UNINSTALLED_START", raising=False)
    monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])
    installs: list = []
    monkeypatch.setattr(gateway_windows, "install", lambda **kw: installs.append(kw))
    spawned: list = []
    monkeypatch.setattr(
        gateway_windows, "_spawn_detached", lambda *a, **kw: spawned.append(1) or 777
    )

    with pytest.raises(gateway_windows.GatewayServiceNotInstalledError):
        gateway_windows.start()

    assert spawned == []
    assert installs == []


# ---------------------------------------------------------------------------
# stop() drain semantics — issue #33778
#
# Background: on Windows, asyncio.add_signal_handler raises NotImplementedError,
# so the gateway's SIGTERM handler (which drains in-flight agents and writes
# resume_pending=True) never fires when `hermes gateway stop` kills the
# process. The fix: stop() writes the planned_stop_marker first, waits for
# the gateway's marker-watcher thread to drain + exit cleanly, then escalates
# to taskkill if drain times out.
# ---------------------------------------------------------------------------








