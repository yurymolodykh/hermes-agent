"""BasicAuthProvider — username/password dashboard auth (no OAuth IDP).

Login is a credential form (``supports_password`` + ``complete_password_login``); cookies,
verify, refresh, ws-tickets and logout are the shared framework. Sessions are stateless
HMAC-signed tokens (no IDP, no database); passwords use stdlib scrypt and login always hashes
even for an unknown username (no username-enumeration timing oracle). Config: ``dashboard.
basic_auth.{username,password_hash|password,secret,session_ttl_seconds}`` or the
``HERMES_DASHBOARD_BASIC_AUTH_*`` env vars (env wins when non-empty; see ``_settings``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
from dataclasses import replace
from typing import Optional

from hermes_cli.dashboard_auth import DashboardAuthProvider, InvalidCredentialsError, RefreshExpiredError, Session
from hermes_cli import dashboard_users as users_mod
from plugins.dashboard_auth._shared import (
    NonInteractiveMixin, SkipRegistration, load_config_section, register_provider, resolve_env_or_cfg)

logger = logging.getLogger(__name__)
_TAG = "dashboard-auth-basic"

# The middleware transparently refreshes via the refresh token when the
# access token lapses, so the TTL controls refresh frequency, not login length.
# Both lifetimes resolve through ``hermes_acl_identity``: an explicit
# ``dashboard.basic_auth.session_ttl_seconds`` wins, and multi-user mode imposes seven days for
# both so a revoked person cannot rotate their way back in.
_DEFAULT_TTL_SECONDS = 12 * 60 * 60
_REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60

# Interactive-login scrypt parameters (~16 MiB, a few ms); n must be a power of two.
# ``hash_password``/``_verify_password`` stay here (public API, used by operators and tests);
# the users file shares the same implementation through hermes_cli.dashboard_users.
_SCRYPT_N = users_mod.SCRYPT_N
_SCRYPT_R = users_mod.SCRYPT_R
_SCRYPT_P = users_mod.SCRYPT_P
_SCRYPT_DKLEN = users_mod.SCRYPT_DKLEN
_SCRYPT_SALT_BYTES = users_mod.SCRYPT_SALT_BYTES

# HMAC-SHA256 digest is appended to signed tokens as a fixed-length suffix
# (no separator — binary HMAC bytes can't be confused with a delimiter).
_SIG_LEN = hashlib.sha256().digest_size

LAST_SKIP_REASON: str = ""


# ---- Password hashing (stdlib scrypt) ----

def hash_password(password: str) -> str:
    """Return a ``scrypt$n$r$p$<salt_b64>$<dk_b64>`` hash string. Public so operators can
    precompute ``password_hash`` for config.yaml (the plaintext then never sits at rest):
    ``python -c "from plugins.dashboard_auth.basic import hash_password; print(hash_password('pw'))"``.

    Delegates to :mod:`hermes_cli.dashboard_users`, so the plugin, the users file and
    ``hermes auth users`` write one format through one implementation.
    """
    return users_mod.hash_password(password)


def _verify_password(password: str, encoded: str) -> bool:
    """Constant-time scrypt verify. False on any malformed hash string.

    Deliberately scrypt-ONLY, as it always was: an imported bcrypt/argon2 hash is handled by the
    users-file path, where the same call can also rewrite it in our format.
    """
    return users_mod.verify_scrypt(password, encoded)


# Verified against when the username is unknown so "no such user" and "wrong
# password" take comparable time.
_DUMMY_HASH = users_mod.dummy_scrypt_hash()


# ---- Token signing (stateless HMAC-signed blobs) ----

def _sign(payload: dict, secret: bytes) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(secret, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + sig).decode()


def _unsign(token: str, secret: bytes, kind: str) -> Optional[dict]:
    """Return the payload if the signature is valid, ``kind`` matches and it
    is unexpired; ``None`` otherwise (including on any decode error)."""
    try:
        blob = base64.urlsafe_b64decode(token.encode())
        if len(blob) <= _SIG_LEN:
            return None
        raw, sig = blob[:-_SIG_LEN], blob[-_SIG_LEN:]
        expected = hmac.new(secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(raw)
    except Exception:
        return None
    if payload.get("kind") != kind or payload.get("exp", 0) <= int(time.time()):
        return None
    return payload


# ---- Provider ----

class BasicAuthProvider(NonInteractiveMixin, DashboardAuthProvider):
    """Username/password provider with stateless HMAC-signed sessions.

    Two credential sources, one login path:

    * ``users_file`` (``dashboard.basic_auth.users_file``) — many people, looked up by login or
      alias. A record found here is authoritative for that login; a login it does not know falls
      through to the single pair below, so existing installations keep working unchanged.
    * ``username``/``password_hash`` — the single pair, exactly as before.
    """

    name = "basic"
    display_name = "Username & Password"
    supports_password = True
    _NOT_INTERACTIVE = "BasicAuthProvider is password-only; use complete_password_login."
    _NO_START_LOGIN = (
        "BasicAuthProvider is password-only; there is no OAuth redirect flow. "
        "The login page POSTs to /auth/password-login instead.")

    def __init__(self, *, username: str, password_hash: str, secret: bytes, ttl_seconds: int = 0,
                 users_file: str = "") -> None:
        if not username and not users_file:
            raise ValueError("username must be non-empty when no users_file is configured")
        if username and not password_hash:
            raise ValueError("password_hash must be non-empty")
        if len(secret) < 16:
            raise ValueError("secret must be at least 16 bytes")
        self._username = username
        self._password_hash = password_hash
        self._has_single_pair = bool(username and password_hash)
        self._secret = secret
        # Lifetimes resolve through ``hermes_acl_identity``: an explicit
        # ``session_ttl_seconds`` wins; without one, multi-user mode gives the access and refresh
        # tokens seven days each so a revoked person cannot rotate their way past a generation.
        from hermes_acl_identity import refresh_ttl_seconds, session_ttl_seconds

        self._ttl = max(60, int(ttl_seconds)) if int(ttl_seconds or 0) > 0 else session_ttl_seconds(
            0, default_seconds=_DEFAULT_TTL_SECONDS)
        self._refresh_ttl = refresh_ttl_seconds(0, default_seconds=_REFRESH_TTL_SECONDS)
        self._users = users_mod.UserStore(users_mod.resolve_users_file_path(users_file))

        self._reported_unknown_format: set[str] = set()

    # ---- users file --------------------------------------------------------

    def users_snapshot(self) -> users_mod.UsersLoadResult:
        """The current users file as loaded (cached until it changes on disk)."""
        return self._users.load()

    def users_problems(self) -> list[users_mod.UsersProblem]:
        return list(self._users.load().problems)

    # ---- password login ----------------------------------------------------

    def complete_password_login(self, *, username: str, password: str) -> Session:
        """Authenticate *username* against the users file, then the single pair.

        Timing contract, on EVERY path, exactly one scrypt work unit and one
        :func:`hmac.compare_digest` on the login:

        * users-file record, scrypt hash — one scrypt verify;
        * users-file record, bcrypt/argon2 hash — the foreign verify plus one scrypt (the re-hash
          on success, a dummy verify on failure);
        * users-file record, unrecognised hash — one scrypt verify against the dummy;
        * a login the file does not know — the single pair exactly as before, one scrypt verify.

        So a login that is unknown, disabled, wrong-password, or in a foreign format all cost the
        same single scrypt pass.
        """
        record = self._find_user(username)
        if record is not None:
            password_ok = self._verify_user_record(record, password)
            if not record.disabled and password_ok:
                return self._mint_session(record.login, from_file=True, generation=record.gen)
            raise InvalidCredentialsError("invalid username or password")

        # No users-file record: the single configured pair, exactly as before. Adding a users file
        # does NOT retire the original login — an existing installation keeps working unchanged.
        if not self._has_single_pair:
            # A users-file-only deployment: still pay one scrypt verify so an unknown login costs
            # the same as a known one.
            _verify_password(password, self._dummy())
            raise InvalidCredentialsError("invalid username or password")
        username_ok = hmac.compare_digest(username.encode("utf-8"), self._username.encode("utf-8"))
        password_ok = _verify_password(password, self._password_hash if username_ok else self._dummy())
        if not (username_ok and password_ok):
            raise InvalidCredentialsError("invalid username or password")
        return self._mint_session(self._username, from_file=False, generation=None)

    @staticmethod
    def _dummy() -> str:
        """The throwaway hash, built once at import so the FIRST unknown login is not twice as
        slow as every later one."""
        return _DUMMY_HASH

    def _find_user(self, username: str) -> Optional[users_mod.UserRecord]:
        """The users-file record for *username*, or None (also when the file is unusable).

        An unusable file yields None for every login, which routes everyone to the single pair
        (fail-closed with respect to the file, and never a half-loaded table).
        """
        loaded = self._users.load()
        if loaded.ok:
            return loaded.by_login(username)
        if loaded.exists and loaded.problems:
            logger.error(
                "dashboard-auth-basic: users file %s is unusable (%s); no user from it may sign in.",
                loaded.path, "; ".join(str(p) for p in loaded.problems))
        return None

    def _verify_user_record(self, record: users_mod.UserRecord, password: str) -> bool:
        """Verify *password* for *record* at a cost of exactly one scrypt work unit.

        A foreign format that verifies is rewritten in scrypt form in the same pass, so the
        migration costs the login nothing extra and needs no separate command.
        """
        kind = users_mod.hash_kind(record.password_hash)

        if kind == users_mod.HASH_UNKNOWN:
            users_mod.verify_scrypt(password, self._dummy())
            self._report_unknown_format(record)
            return False

        if kind == users_mod.HASH_SCRYPT:
            return users_mod.verify_scrypt(password, record.password_hash)

        # bcrypt / argon2: read-only formats from the import.
        try:
            ok = users_mod.verify_password(password, record.password_hash)
        except users_mod.UnsupportedHashFormat as exc:
            logger.error("dashboard-auth-basic: login %r cannot be verified: %s", record.login, exc)
            users_mod.verify_scrypt(password, self._dummy())
            return False

        if not ok:
            # Pay the scrypt unit a scrypt user's failed attempt pays, so a foreign-format
            # account's wrong-password path is not measurably cheaper.
            users_mod.verify_scrypt(password, self._dummy())
            return False

        # Success: this scrypt hash IS the cost, and it is also the rewrite.
        self._rehash_record(record, password)
        return True

    def _report_unknown_format(self, record: users_mod.UserRecord) -> None:
        """One warning line per login, naming the login and never the hash."""
        if record.login in self._reported_unknown_format:
            return
        self._reported_unknown_format.add(record.login)
        logger.warning(
            "dashboard-auth-basic: unknown password hash format for login %r; this person cannot "
            "sign in until the hash is rewritten (hermes auth users passwd).", record.login)

    def _rehash_record(self, record: users_mod.UserRecord, password: str) -> None:
        """Rewrite *record*'s verified hash in scrypt form, atomically."""
        try:
            updated = replace(record, password_hash=users_mod.hash_password(password))
            if not self._users.replace_record(updated):
                logger.warning(
                    "dashboard-auth-basic: login %r verified but its record vanished before the "
                    "hash could be re-written; the old hash stays in place.", record.login)
                return
            logger.info(
                "dashboard-auth-basic: login %r signed in with a %s hash, re-written as scrypt.",
                record.login, users_mod.hash_kind(record.password_hash))
        except Exception:
            # A failed rewrite must never fail a login that already verified.
            logger.exception(
                "dashboard-auth-basic: could not re-write the hash for login %r; it stays in its "
                "old format and will be retried on the next sign-in.", record.login)

    # ---- session lifecycle -------------------------------------------------

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        """A signed access token, or ``None`` — including when the credential behind it has been
        revoked. Every request passes through here, so the generation check lands at the one place
        that already decides "this request is signed in"."""
        payload = _unsign(access_token, self._secret, "access")
        if payload is None:
            return None
        user_id = str(payload.get("sub", ""))
        from hermes_acl_identity import credential_valid

        if not credential_valid(user_id, payload.get("gen"), from_file=payload.get("src")):
            logger.info("dashboard-auth-basic: refused a session for %r (%s)", user_id,
                        _revocation_reason(user_id, payload))
            return None
        return self._session(user_id, int(payload["exp"]), access_token, "",
                             generation=payload.get("gen"))

    def credential_status(self, token: str) -> tuple[str, str]:
        """``(status, detail)`` for a presented access token, for the gate's refusal message.

        ``("ok"|"unknown", "")`` when nothing is wrong with the *credential* (an expired or
        foreign token is ``unknown``: that is the ordinary re-login case), and
        ``("revoked", "<rule>")`` when the token is ours but the person's generation, disabled
        flag or record has moved past it. The gate turns the second into a distinct 401 so an
        operator sees "your access was revoked" instead of a generic expiry.
        """
        payload = _unsign(token, self._secret, "access")
        if payload is None:
            return "unknown", ""
        from hermes_acl_identity import revoke_reason

        reason = revoke_reason(str(payload.get("sub", "")), payload.get("gen"),
                               from_file=payload.get("src"))
        return ("ok", "") if reason == "accepted" else ("revoked", reason)

    def refresh_session(self, *, refresh_token: str) -> Session:
        """Rotate a lapsed session. Refuses (``RefreshExpiredError``) once the credential that
        minted it is revoked: a refresh token must not outlive the generation that revoked
        the person, or seven days becomes thirty for somebody who was fired yesterday."""
        if not refresh_token:
            raise RefreshExpiredError("no refresh token present in session")
        payload = _unsign(refresh_token, self._secret, "refresh")
        if payload is None:
            raise RefreshExpiredError("refresh token expired or invalid")
        user_id = str(payload.get("sub", self._username))
        from hermes_acl_identity import credential_valid

        if not credential_valid(user_id, payload.get("gen"), from_file=payload.get("src")):
            raise RefreshExpiredError("session credential revoked")
        return self._mint_session(user_id, from_file=payload.get("src"), generation=payload.get("gen"))

    def revoke_session(self, *, refresh_token: str) -> None:
        # Stateless tokens — nothing to revoke server-side; the session expires within its TTL. Must not raise.
        return None

    # ---- internals ---------------------------------------------------------

    def _mint_session(self, user_id: str, *, from_file=None, generation=None) -> Session:
        # ``jti`` gives every session its own tokens, even two minted in one second: a listener that
        # drops a session's tokens (the Webapp's sign-out) must not get them back from a later login.
        now = int(time.time())
        exp = now + self._ttl
        jti = secrets.token_urlsafe(16)
        claims = {"sub": user_id, "jti": jti, "src": bool(from_file) if from_file is not None else False}
        if generation is not None:
            claims["gen"] = int(generation)
        return self._session(
            user_id, exp,
            _sign({**claims, "kind": "access", "exp": exp}, self._secret),
            _sign({**claims, "kind": "refresh", "exp": now + self._refresh_ttl}, self._secret),
            generation=claims.get("gen"))

    def _session(self, user_id: str, exp: int, access_token: str, refresh_token: str,
                 *, generation=None) -> Session:
        return Session(
            user_id=user_id, email="", display_name=user_id, org_id="", provider=self.name,
            expires_at=exp, access_token=access_token, refresh_token=refresh_token,
            credential_generation=None if generation is None else int(generation))


def _revocation_reason(user_id: str, payload: dict) -> str:
    """Audit-safe explanation of a refused session: which rule fired, never the credential."""
    from hermes_acl_identity import revoke_reason

    return revoke_reason(user_id, payload.get("gen"), from_file=payload.get("src"))


# ---- Plugin entry point ----

def _load_config_basic_auth_section() -> dict:
    return load_config_section(logger, _TAG, "dashboard", "basic_auth")


# Shell syntax that was written to .env/config without being run, e.g. a
# ``$(openssl rand -base64 32)`` line inside a quoted heredoc, or the command (or a doc
# placeholder naming it) pasted in place of its output. Base64 and hex never contain
# these (nor a space), and everyone who copied the same line would share the same key.
_UNEXPANDED_SHELL_MARKERS = ("$(", "`", "openssl rand")

# The error above only reaches the log files, not the console of ``hermes serve`` /
# ``hermes dashboard``; one stderr line per process makes it visible there too.
_shell_text_secret_reported = False


def _resolve_secret(cfg_section: dict) -> bytes:
    """Resolve the token-signing secret (base64, hex, or raw text). When unset, generates
    a random per-process secret (sessions then don't survive a restart or span multiple
    workers — logged at INFO). A value holding unexpanded shell text is never used as the
    key: it is logged as an error and replaced by a random per-process secret."""
    raw = resolve_env_or_cfg("HERMES_DASHBOARD_BASIC_AUTH_SECRET", cfg_section.get("secret"))
    if not raw:
        logger.info(
            "dashboard-auth-basic: no 'secret' configured; generating a random "
            "per-process signing key. Sessions will not survive a restart or span "
            "multiple workers. Set dashboard.basic_auth.secret (or "
            "HERMES_DASHBOARD_BASIC_AUTH_SECRET) for stable sessions.")
        return secrets.token_bytes(32)
    if any(marker in raw for marker in _UNEXPANDED_SHELL_MARKERS):
        logger.error(
            "dashboard-auth-basic: the configured signing secret contains unexpanded shell "
            "text (such as '$(openssl rand -base64 32)') instead of a random value, so it "
            "is ignored and a random per-process signing key is used. Set "
            "HERMES_DASHBOARD_BASIC_AUTH_SECRET (or dashboard.basic_auth.secret) to the "
            "output of 'openssl rand -base64 32' to keep sessions across restarts.")
        global _shell_text_secret_reported
        if not _shell_text_secret_reported:
            _shell_text_secret_reported = True
            print(
                "hermes: dashboard login: HERMES_DASHBOARD_BASIC_AUTH_SECRET (or "
                "dashboard.basic_auth.secret) holds shell text instead of a random value; "
                "using a per-process key, so sessions end on restart. Replace it with the "
                "output of: openssl rand -base64 32", file=sys.stderr, flush=True)
        return secrets.token_bytes(32)
    for decoder in (base64.b64decode, bytes.fromhex):
        try:
            decoded = decoder(raw)
            if len(decoded) >= 16:
                return decoded
        except (ValueError, TypeError):
            pass
    return raw.encode("utf-8")


def _settings() -> dict:
    """Resolve BasicAuthProvider kwargs from env/config; raises ``SkipRegistration``.

    ``users_file`` is a SECOND source. When it is configured the provider registers even with no
    ``username``/``password_hash`` pair, and when a pair is also configured both keep working.
    """
    section = _load_config_basic_auth_section()

    def setting(env_name: str, cfg_key: str) -> str:
        return resolve_env_or_cfg(env_name, section.get(cfg_key, ""))

    username = setting("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "username")
    password_hash = setting("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH", "password_hash")
    plaintext = setting("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "password")
    ttl_raw = setting("HERMES_DASHBOARD_BASIC_AUTH_TTL_SECONDS", "session_ttl_seconds")
    users_file = str(section.get("users_file") or "").strip()

    if users_file:
        users_path = users_mod.resolve_users_file_path(users_file)
        try:
            loaded = users_mod.load_users_file(users_path)
        except users_mod.UsersFilePermissionError as exc:
            # Refusing to load is the point: say why, loudly, and do not register.
            raise SkipRegistration(str(exc), level="error") from exc
        if not loaded.exists:
            logger.warning(
                "dashboard-auth-basic: dashboard.basic_auth.users_file is set to %s but the file "
                "does not exist; only the single username/password pair (if any) can sign in.",
                users_path)
        for problem in loaded.problems:
            logger.error("dashboard-auth-basic: users file %s: %s", users_path, problem)
        for warning in loaded.warnings:
            logger.warning("dashboard-auth-basic: users file: %s", warning)
        if loaded.exists and loaded.problems:
            logger.error(
                "dashboard-auth-basic: users file %s is unusable; NO user from it may sign in "
                "until it is fixed. The single username/password pair still works.", users_path)

    if not username and not users_file:
        raise SkipRegistration(
            "dashboard.basic_auth.username is not set (and HERMES_DASHBOARD_BASIC_AUTH_USERNAME "
            "is empty). Set a username and a password (or password_hash) under "
            "dashboard.basic_auth in config.yaml to enable username/password dashboard "
            "login, or use the OAuth provider, or pass --insecure to skip the auth gate.")
    if username and not password_hash and not plaintext:
        if users_file:
            # The pair is half-configured but a users file carries the logins: use the file and
            # say the pair is ignored, rather than refusing to start.
            logger.warning(
                "dashboard-auth-basic: dashboard.basic_auth.username is set without a password; "
                "the single pair is ignored and only users_file logins can sign in.")
            username = ""
        else:
            raise SkipRegistration(
                "dashboard.basic_auth.username is set but neither password_hash nor password "
                "is configured. Provide one of them (password_hash is preferred — compute it "
                "with plugins.dashboard_auth.basic.hash_password).",
                level="warning")
    # Precedence: env password (hashed in-memory) overrides any config password_hash so
    # operators can rotate without editing config; a config password_hash wins over a
    # config-only plaintext password (preferred at-rest form). A users-file-only deployment has
    # no username here, so the whole pair is skipped rather than hashed from an empty string.
    if username:
        plaintext_from_env = os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "").strip()
        if plaintext_from_env:
            password_hash = hash_password(plaintext_from_env)
            logger.info("dashboard-auth-basic: hashed env-supplied password in-memory (overrides any config password_hash).")
        elif not password_hash:
            password_hash = hash_password(plaintext)
            logger.info(
                "dashboard-auth-basic: hashed plaintext password in-memory. "
                "For production, precompute dashboard.basic_auth.password_hash "
                "and remove the plaintext password from config.")
    # 0 = "not configured": the provider resolves the lifetime through ``hermes_acl_identity``
    # (an explicit number wins; multi-user mode gives 7 days; otherwise the 12-hour default), so
    # baking the default in HERE would silently defeat the multi-user rule.
    try:
        ttl = int(ttl_raw) if ttl_raw else 0
    except ValueError:
        ttl = 0
    return {"username": username, "password_hash": password_hash, "secret": _resolve_secret(section),
            "ttl_seconds": ttl, "users_file": users_file}


def register(ctx) -> None:
    """Register ``BasicAuthProvider`` when a single pair or a users file provides logins; a no-op
    for OAuth / ``--insecure`` setups."""
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""
    kwargs, LAST_SKIP_REASON = register_provider(ctx, logger, _TAG, BasicAuthProvider, _settings)
    if kwargs is not None:
        logger.info("dashboard-auth-basic: registered password provider (username=%s, users_file=%s)",
                    kwargs["username"] or "-", kwargs["users_file"] or "-")
