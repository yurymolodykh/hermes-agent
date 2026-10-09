"""Materialize the isolated fast-restart e2e test profile into a fresh temp dir.

The checked-in template under ``test-profile/`` is NEVER used directly as
HERMES_HOME.  This script copies it into a brand-new temporary directory for
each run so that:

  * HERMES_HOME always points at a fresh temp dir (never production);
  * throwaway API keys are generated per run (nothing real is committed);
  * the production gateway port 8642 is never touched (api_server runs on
    127.0.0.1:18642 by default and the profile contains no 8642 reference).

Layout of the materialized root::

    <temp>/hermes-e2e/<stamp>/            <- kanban home (HERMES_KANBAN_HOME)
        kanban/.gateway_generation        <- written by the gateway
        kanban.db                         <- created by the gateway
        profiles/test-fast-restart/       <- HERMES_HOME
            config.yaml
            .env                          <- generated keys, API_SERVER_PORT=18642

Usage::

    python bootstrap_test_profile.py [--port 18642] [--mock-port 18643]
                                     [--print-home]

``--print-home`` prints only the profile dir path (for cmd batch capture);
otherwise a short JSON summary is printed to stdout.

Importable::

    from bootstrap_test_profile import materialize_test_profile
    profile_dir = materialize_test_profile()
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
import time
from pathlib import Path

PROFILE_NAME = "test-fast-restart"
PROD_GATEWAY_PORT = 8642  # never bound by this profile / harness

_TEMPLATE_DIR = Path(__file__).resolve().parent / "test-profile"


def materialize_test_profile(
    api_port: int = 18642,
    mock_port: int = 18643,
    root: Path | None = None,
) -> Path:
    """Copy the isolated profile template into a fresh temp HERMES_HOME.

    Returns the profile dir path (``<root>/profiles/test-fast-restart``).
    ``root`` defaults to a fresh ``<temp>/hermes-e2e/<stamp>`` directory; the
    caller may pin it (the e2e harness does) to share one kanban home.
    """
    if api_port == PROD_GATEWAY_PORT:
        raise RuntimeError(
            f"refusing to run the isolated test profile on production port "
            f"{PROD_GATEWAY_PORT}"
        )
    if root is None:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base_tmp = Path(tempfile.gettempdir()) / "hermes-e2e"
        base_tmp.mkdir(parents=True, exist_ok=True)
        root = base_tmp / stamp
    root.mkdir(parents=True, exist_ok=True)
    (root / "kanban").mkdir(parents=True, exist_ok=True)

    profile_dir = root / "profiles" / PROFILE_NAME
    profile_dir.mkdir(parents=True, exist_ok=True)

    # config.yaml: template with the mock LLM port substituted.
    cfg = (_TEMPLATE_DIR / "config.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace("__MOCK_PORT__", str(mock_port))
    (profile_dir / "config.yaml").write_text(cfg, encoding="utf-8")

    # .env: template with throwaway random keys + the api_server port.
    api_key = secrets.token_urlsafe(32)
    mock_key = secrets.token_urlsafe(16)
    env = (_TEMPLATE_DIR / ".env.template").read_text(encoding="utf-8")
    env = (
        env.replace("__API_PORT__", str(api_port))
        .replace("__API_KEY__", api_key)
        .replace("__MOCK_KEY__", mock_key)
    )
    (profile_dir / ".env").write_text(env, encoding="utf-8")

    return profile_dir


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=18642,
                    help="api_server test port (default 18642)")
    ap.add_argument("--mock-port", type=int, default=18643,
                    help="mock LLM port (default 18643)")
    ap.add_argument("--print-home", action="store_true",
                    help="print only the profile dir path")
    a = ap.parse_args(argv)

    profile_dir = materialize_test_profile(api_port=a.port, mock_port=a.mock_port)
    if a.print_home:
        print(profile_dir)
        return 0
    print(json.dumps({
        "profile": PROFILE_NAME,
        "hermes_home": str(profile_dir),
        "kanban_home": str(profile_dir.parent.parent),
        "api_port": a.port,
        "prod_port_untouched": PROD_GATEWAY_PORT,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
