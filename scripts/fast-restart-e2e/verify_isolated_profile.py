"""Verify the isolated fast-restart e2e test profile meets its acceptance bar.

Checks (independent, each reported with PASS/FAIL):

  1. profile-template-exists       — test-profile/ template is present
  2. no-prod-port-8642             — no "8642" reference in template nor in a
                                     materialized profile
  3. api-server-only               — no telegram / yadisk platform config; the
                                     only platform-capable settings are
                                     api_server env-driven ones
  4. api-port-18642                — API_SERVER_PORT defaults to 18642 and the
                                     materialized .env carries it
  5. no-real-telegram-keys         — no TELEGRAM_*/YADISK_* credential vars and
                                     no obvious secret-shaped values (the
                                     materialized keys are freshly generated)
  6. temp-hermes-home              — bootstrap materializes under a fresh temp
                                     dir (never production HERMES_HOME)

Usage::

    python verify_isolated_profile.py            # verify template + a throwaway
                                                 # materialization (default)
    python verify_isolated_profile.py --dir PATH # verify an existing profile dir

Exit code 0 when all checks pass, 1 otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

REPO_SCRIPTS = Path(__file__).resolve().parent
TEMPLATE_DIR = REPO_SCRIPTS / "test-profile"
PROD_PORT = "8642"
API_PORT = "18642"

# Platform families we must NOT configure at all for this isolated profile.
# The acceptance bar is "no real Telegram messages can be sent" / "no real
# Telegram/yadisk credentials" — so the credential scan is scoped to these
# prefixes only (API_SERVER_KEY / MOCK_API_KEY are throwaway per-run keys and
# are expected to be present).
_FORBIDDEN_PLATFORM_MARKERS = ("telegram", "yadisk")
_FORBIDDEN_CRED_PREFIXES = ("TELEGRAM_", "YADISK_")


def _walk_texts(root: Path, strip_comments: bool = True) -> list[str]:
    """Read text files under ``root``, optionally dropping comment lines.

    ``#``-prefixed lines are prose/commentary (they often *describe* the
    isolation guarantees, e.g. "no reference to 8642") and must not be counted
    as configuration.  Only active config lines are scanned for the production
    port and forbidden platforms.
    """
    out = []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".yaml", ".yml", ".env", ".template", ".md", ".cmd"):
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            if strip_comments:
                lines = [
                    ln for ln in lines
                    if ln.strip() and not ln.lstrip().startswith(("#", "//", "<", "REM"))
                ]
            out.append("\n".join(lines))
    return out


def _real_secrets_found(texts: list[str]) -> list[str]:
    """Return forbidden-platform (telegram/yadisk) env lines with a non-placeholder value."""
    hits = []
    for text in texts:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip().upper()
            val = val.strip()
            if not any(key.startswith(p) for p in _FORBIDDEN_CRED_PREFIXES):
                continue
            if val.startswith("__") and val.endswith("__"):
                continue  # placeholder
            if len(val) >= 16:
                hits.append(f"{key}={val[:12]}…")
    return hits


def _platform_leak(texts: list[str]) -> list[str]:
    """Return forbidden platform markers that indicate ENABLED config.

    ``telegram_delivery_policy: off`` is a safety switch (it deliberately
    disables outbound delivery) and is NOT a leak.  Flag only genuine
    enablement: an exact telegram/yadisk credential var (TELEGRAM_BOT_TOKEN,
    TELEGRAM_API_ID, YADISK_OAUTH_TOKEN, ...) or an explicit
    ``platforms.<p>.enabled: true`` / ``<p>.enabled: true`` block.
    """
    credential_keys = (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH",
        "TELEGRAM_CHAT_ID", "TELEGRAM_THREAD_ID", "TELEGRAM_ALLOWED_USERS",
        "YADISK_OAUTH_TOKEN", "YADISK_CLIENT_ID", "YADISK_CLIENT_SECRET",
        "YADISK_TOKEN",
    )
    hits = []
    for text in texts:
        low = text.lower()
        upper = text.upper()
        for marker in _FORBIDDEN_PLATFORM_MARKERS:
            if marker in hits:
                continue
            # Exact credential var for THIS platform.
            if any(k.startswith(f"{marker.upper()}_") for k in credential_keys) and any(
                k in upper for k in credential_keys if k.startswith(f"{marker.upper()}_")
            ):
                hits.append(marker)
                continue
            # Explicit enabled: true in a platform-named section or platforms block.
            if re.search(rf"(^|\s){marker}\s*:\s*\{{.*?enabled\s*:\s*true", low, re.DOTALL):
                hits.append(marker)
                continue
            if re.search(rf"platforms\s*:\s*[\s\S]*?{marker}\s*:\s*[\s\S]*?enabled\s*:\s*true", low):
                hits.append(marker)
                continue
    return sorted(set(hits))


def verify_profile(profile_dir: Path | None = None) -> list[dict]:
    checks: list[dict] = []
    passed: list[str] = []
    if profile_dir is None:
        from bootstrap_test_profile import materialize_test_profile

        profile_dir = materialize_test_profile(api_port=int(API_PORT))

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "pass": bool(ok), "detail": detail})
        if ok:
            passed.append(name)

    # 1. template exists
    tpl_ok = (TEMPLATE_DIR / "config.yaml").is_file() and (TEMPLATE_DIR / ".env.template").is_file()
    add("profile-template-exists", tpl_ok, str(TEMPLATE_DIR))

    # 2. no production port reference (template)
    tpl_texts = _walk_texts(TEMPLATE_DIR)
    tpl_has_8642 = any(PROD_PORT in t for t in tpl_texts)
    add("no-prod-port-8642-template", not tpl_has_8642,
        "no '8642' in test-profile/ template")

    # 3 + 4 + 5 on materialized profile
    mat_texts = _walk_texts(profile_dir)
    mat_has_8642 = any(PROD_PORT in t for t in mat_texts)
    add("no-prod-port-8642-materialized", not mat_has_8642,
        f"no '8642' in {profile_dir}")

    leak = _platform_leak(mat_texts + tpl_texts)
    add("api-server-only", not leak,
        f"no telegram/yadisk config anywhere" + (f" (found: {leak})" if leak else ""))

    env_path = profile_dir / ".env"
    env_txt = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    port_line = next((ln for ln in env_txt.splitlines() if ln.startswith("API_SERVER_PORT=")), "")
    add("api-port-18642", port_line == f"API_SERVER_PORT={API_PORT}", port_line or "API_SERVER_PORT missing")

    host_line = next((ln for ln in env_txt.splitlines() if ln.startswith("API_SERVER_HOST=")), "")
    add("api-host-loopback", host_line == "API_SERVER_HOST=127.0.0.1", host_line or "API_SERVER_HOST missing")

    real = _real_secrets_found(mat_texts + tpl_texts)
    add("no-real-telegram-keys", not real,
        f"no real credential values" + (f" (found: {real})" if real else ""))

    # 6. temp HERMES_HOME (fresh temp dir, not production profiles home)
    home_str = str(profile_dir)
    prod_home = str(Path.home() / ".hermes").lower()
    under_temp = "hermes-e2e" in home_str or Path(tempfile.gettempdir()).resolve() in Path(home_str).resolve().parents
    not_prod = prod_home not in home_str.replace("\\", "/").lower()
    add("temp-hermes-home", under_temp and not_prod,
        f"HERMES_HOME={home_str}; under temp & not the default profile home")

    return checks


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=None, help="verify an existing profile dir instead of materializing one")
    a = ap.parse_args(argv)
    profile_dir = Path(a.dir) if a.dir else None
    checks = verify_profile(profile_dir)

    all_ok = all(c["pass"] for c in checks)
    for c in checks:
        tag = "PASS" if c["pass"] else "FAIL"
        line = f"[{tag}] {c['name']}"
        if c.get("detail"):
            line += f" — {c['detail']}"
        print(line)
    print(f"ALL CHECKS PASSED: {all_ok}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
