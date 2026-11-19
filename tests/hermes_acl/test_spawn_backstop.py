"""The backstop test: an unclassified command-spawn site fails the build.

Point 3 of the task, done as a check rather than a claim. The archive's ``tool_firewall`` guards
a DENYLIST config where a regression re-enables a tool. Our config never disables tools, so the
regression that matters is different: someone adds a place that runs a command string and applies
no boundary. Nothing breaks at runtime — the child simply runs unconfined — so only a static
check catches it.

Three assertions, each closing one way the inventory can lie:

1. **nothing uncovered** — every command-spawn site the scanner finds has an entry;
2. **nothing stale** — no entry survives for a file that no longer spawns (the list cannot rot);
3. **nothing unexplained** — every entry says either that it is confined (and by what) or why it
   is out of scope.

Plus a NEGATIVE test: a synthetic site inserted into a scratch tree must be reported as
uncovered. Without it, the test could pass because the scanner stopped finding anything at all —
exactly the failure mode of a check that "goes quiet".
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from hermes_acl import spawn_inventory as inv

REPO = Path(__file__).resolve().parents[2]
INVENTORY = REPO / "hermes_acl" / "spawn_inventory.json"


@pytest.fixture(scope="module")
def sites():
    return inv.scan_all()


@pytest.fixture(scope="module")
def inventory():
    return inv.load_inventory(INVENTORY)


def test_inventory_file_exists_and_parses():
    raw = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["files"], "the inventory cannot be empty — an empty list passes every check"


def test_scanner_finds_the_known_hot_paths(sites):
    """The scanner must SEE the terminal paths. A scanner that finds nothing makes the rest moot."""
    paths = {s.path for s in sites}
    assert "tools/environments/local.py" in paths, (
        "the foreground terminal path must be in the inventory; if this fails the scanner is "
        "broken, not the code")
    assert "tools/process_registry.py" in paths
    builders = {s.path for s in sites if s.reason == "shell-command-builder"}
    assert builders, "argv builders must be found: the terminal spawns pass argv as a parameter"


def test_every_command_spawn_site_is_classified(sites, inventory):
    uncovered, _stale, _bad = inv.audit(inventory, sites)
    assert not uncovered, (
        "unclassified command-spawn site(s):\n  " + "\n  ".join(uncovered) +
        "\nAdd an entry to hermes_acl/spawn_inventory.json: either wrap the argv with "
        "hermes_acl.policy.wrap_argv (status 'confined') or record why it is out of scope.")


def test_no_stale_inventory_entries(sites, inventory):
    _uncovered, stale, _bad = inv.audit(inventory, sites)
    assert not stale, (
        "inventory entries with no command-spawn site any more (remove or re-check them):\n  "
        + "\n  ".join(stale))


def test_every_entry_explains_itself(sites, inventory):
    _uncovered, _stale, bad = inv.audit(inventory, sites)
    assert not bad, "inventory problems:\n  " + "\n  ".join(bad)


def test_confined_entries_really_call_the_wrapper():
    """A 'confined' entry must be TRUE: the file has to reference wrap_argv.

    Otherwise the inventory becomes a place where a comfortable word replaces a boundary — the
    entry says "confined", nobody checks, and the site runs unconfined.
    """
    inventory = inv.load_inventory(INVENTORY)
    failures = []
    for path, entry in inventory.items():
        if entry["status"] != "confined":
            continue
        source = (REPO / path).read_text(encoding="utf-8")
        if "wrap_argv" not in source:
            failures.append(f"{path}: marked 'confined' but never calls wrap_argv")
    assert not failures, "\n".join(failures)


def test_a_new_uncovered_site_is_reported(tmp_path):
    """NEGATIVE: a fresh command-spawn site in a scratch tree must be caught."""
    tree = tmp_path / "tools"
    tree.mkdir()
    (tree / "new_tool.py").write_text(textwrap.dedent('''
        import subprocess

        def run(user_command):
            return subprocess.Popen(["bash", "-c", user_command])
    '''), encoding="utf-8")
    found = inv.scan_all(tmp_path)
    assert any(s.path == "tools/new_tool.py" for s in found), (
        "the scanner must find a plain `bash -c <variable>` spawn in a new file")
    uncovered, _stale, _bad = inv.audit({}, found)
    assert uncovered == ["tools/new_tool.py"], uncovered


def test_a_new_uncovered_site_in_the_real_tree_would_fail(tmp_path):
    """The same negative, against the REAL inventory: drop the entry, expect the failure."""
    inventory = inv.load_inventory(INVENTORY)
    victim = "tools/environments/local.py"
    assert victim in inventory
    del inventory[victim]
    uncovered, _stale, _bad = inv.audit(inventory, inv.scan_all())
    assert victim in uncovered, (
        "removing an entry must surface that file as uncovered — this is the assertion that "
        "would have fired if the boundary had been forgotten on the hot path")


def test_a_count_change_is_reported(sites, inventory):
    """Adding a second spawn to a listed file must be visible, not silently absorbed."""
    mutated = {k: dict(v) for k, v in inventory.items()}
    victim = "tools/environments/local.py"
    mutated[victim]["spawn_sites"] += 1
    _uncovered, _stale, bad = inv.audit(mutated, sites)
    assert any(victim in msg and "spawn site" in msg for msg in bad), bad


def test_out_of_scope_entries_are_reviewable():
    """Every out-of-scope reason must be a sentence, not a shrug ('ok', 'n/a', 'nohack')."""
    inventory = inv.load_inventory(INVENTORY)
    weak = [p for p, e in inventory.items()
            if e["status"] == "out-of-scope" and len(str(e.get("reason", "")).split()) < 8]
    assert not weak, f"reasons too thin to review: {weak}"


def test_scanner_is_deterministic(sites):
    """Same tree, same answer twice — a flaky backstop gets muted."""
    again = inv.scan_all()
    assert [(s.path, s.line, s.reason) for s in sites] == [(s.path, s.line, s.reason) for s in again]
