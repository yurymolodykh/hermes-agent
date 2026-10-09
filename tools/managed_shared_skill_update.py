"""Explicit CAS-only maintenance of an existing SKILL.md in configured shared create_dir.

This is not a general-purpose external-directory write API. It writes one existing
file under a verified shared root and keeps a persistent, hash-bound rollback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _error(message: str) -> dict:
    return {"success": False, "error": message}


def _no_redirect(path: Path) -> bool:
    # Also detect Windows junctions (Python 3.12 has Path.is_junction; on
    # earlier versions the core helper additionally probes FILE_ATTRIBUTE_REPARSE_POINT).
    from tools.skill_manager_tool import _is_path_redirect
    return not _is_path_redirect(path)


def _path(root: Path, name: str, relative: str) -> Path:
    from tools.skill_manager_tool import _validate_name
    if _validate_name(name):
        raise ValueError("invalid skill name")
    if not isinstance(relative, str) or "\\" in relative or relative.startswith("/"):
        raise ValueError("shared_path must use a relative POSIX category/name/SKILL.md")
    parts = relative.split("/")
    if len(parts) != 3 or parts[-2:] != [name, "SKILL.md"] or _validate_name(parts[0]):
        raise ValueError("shared_path must be exactly category/name/SKILL.md")
    if any(part in {".", ".."} or part.startswith(".") for part in parts):
        raise ValueError("hidden or traversal component in shared_path")
    category, skill, target = root / parts[0], root / parts[0] / name, root.joinpath(*parts)
    if not all(p.exists() and _no_redirect(p) for p in (root, category, skill, target)):
        raise ValueError("target and parents must exist and contain no symlink/junction")
    if not target.is_file() or target.resolve().relative_to(root.resolve()) != Path(*parts):
        raise ValueError("target is not an in-scope regular file")
    # Exact name is unique across the configured root; an alternate category
    # must not silently become the authoritative loader source.
    matches = []
    for md in root.rglob("SKILL.md"):
        if md.parent.name == name:
            matches.append(md)
    if matches != [target]:
        raise ValueError("shared name is ambiguous or target does not match inventory")
    return target


def _atomic_bytes(path: Path, data: bytes) -> None:
    fd, temp = tempfile.mkstemp(prefix=".managed-skill-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, path.stat().st_mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _journal(path: Path, record: dict) -> None:
    data = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    fd, temp = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def managed_shared_update(root: Path, name: str, shared_path: str,
                          expected_sha256: str, content: str | None = None,
                          rollback_id: str | None = None) -> dict:
    """Publish candidate or restore recorded bytes, never last-writer-wins.

    Rollback is another CAS mutation, not a blind undo. Journals/backups stay
    in the shared root, are identified by generated opaque ids, and are not
    SKILL.md files discovered by the normal loader.
    """
    from tools.skill_manager_tool import _strict_frontmatter
    import yaml

    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        return _error("expected_sha256 must be a lowercase SHA-256 hex digest")
    if (content is None) == (rollback_id is None):
        return _error("supply exactly one of content or rollback_id")
    if rollback_id is not None and not re.fullmatch(r"[0-9a-f]{32}", rollback_id):
        return _error("invalid rollback_id")
    try:
        target = _path(root, name, shared_path)
        prior_fm = _strict_frontmatter(target.read_text(encoding="utf-8"))
        if prior_fm.get("name") != name:
            return _error("existing SKILL.md frontmatter name mismatch")
        if content is not None:
            proposed = _strict_frontmatter(content)
            if proposed.get("name") != name:
                return _error("candidate frontmatter name mismatch")
            candidate = content.encode("utf-8")
        else:
            candidate = None
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return _error(f"invalid shared target or candidate: {exc}")

    history = root / ".managed-shared-history"
    lock = root / f".managed-shared-{name}.lock"
    owner_token = uuid.uuid4().hex
    lock_created = False
    try:
        if history.exists() and (not history.is_dir() or not _no_redirect(history)):
            return _error("unsafe shared history directory")
        history.mkdir(exist_ok=True)
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        lock_created = True
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(owner_token)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        if lock_created:
            try:
                if _no_redirect(lock) and lock.read_text(encoding="ascii") == owner_token:
                    lock.unlink()
            except OSError:
                pass
        return _error(f"could not acquire exclusive shared-skill lock: {exc}")
    backup = None
    journal = None
    try:
        # Revalidate inside lock. Uncooperative direct writers remain outside
        # the transactional guarantee; an immediate CAS check still catches
        # modifications before our replace.
        target = _path(root, name, shared_path)
        before = target.read_bytes()
        before_hash = _sha(before)
        if before_hash != expected_sha256:
            return _error(f"CAS mismatch: current_sha256={before_hash}")
        if rollback_id is not None:
            prior_journal = history / f"{rollback_id}.json"
            prior_backup = history / f"{rollback_id}.bak"
            if not all(p.is_file() and _no_redirect(p) for p in (prior_journal, prior_backup)):
                return _error("rollback record or backup missing/redirected")
            recorded = json.loads(prior_journal.read_text(encoding="utf-8"))
            if (recorded.get("shared_path") != shared_path or
                recorded.get("name") != name or
                recorded.get("after_sha256") != before_hash or
                # Prepared entries are recoverable only when their recorded
                # after hash equals the live CAS hash (swap already landed).
                recorded.get("status") not in {"prepared", "committed"}):
                return _error("rollback record does not match current target")
            candidate = prior_backup.read_bytes()
            if _sha(candidate) != recorded.get("before_sha256"):
                return _error("rollback backup checksum mismatch")
            fm = _strict_frontmatter(candidate.decode("utf-8"))
            if fm.get("name") != name:
                return _error("rollback backup frontmatter name mismatch")
        assert candidate is not None
        after_hash = _sha(candidate)
        if after_hash == before_hash:
            return _error("candidate bytes are identical to existing shared skill")
        # Scan the proposed bytes, never the still-current target. The isolated
        # temporary SKILL.md is removed before any backup/journal/replace, so a
        # blocked candidate cannot disturb either the target or rollback data.
        from tools.skill_manager_tool import _security_scan_skill
        with tempfile.TemporaryDirectory(prefix=".managed-shared-scan-", dir=root) as scan_dir:
            (Path(scan_dir) / "SKILL.md").write_bytes(candidate)
            scan_error = _security_scan_skill(Path(scan_dir), strict=True)
        if scan_error:
            return _error(scan_error)
        transaction = uuid.uuid4().hex
        backup = history / f"{transaction}.bak"
        journal = history / f"{transaction}.json"
        # Exclusive backup creation; a durable copy must exist before overwrite.
        with open(backup, "xb") as stream:
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
        if _sha(backup.read_bytes()) != before_hash:
            return _error("backup readback checksum mismatch")
        record = {"id": transaction, "name": name, "shared_path": shared_path,
                  "before_sha256": before_hash, "after_sha256": after_hash,
                  "status": "prepared", "source_rollback_id": rollback_id}
        _journal(journal, record)
        # Second CAS just before replace; cooperative writers are serialized.
        if _sha(target.read_bytes()) != before_hash:
            return _error("CAS mismatch before replace; backup retained")
        _atomic_bytes(target, candidate)
        observed = _sha(target.read_bytes())
        if observed != after_hash:
            # Do not overwrite a different concurrent writer on rollback.
            if observed == before_hash:
                return _error("post-write checksum mismatch; original still present")
            return _error(f"post-write checksum mismatch; inspect journal {journal}")
        record["status"] = "committed"
        _journal(journal, record)
        return {"success": True, "transaction": record, "target": str(target),
                "backup": str(backup), "journal": str(journal),
                "post_readback": observed}
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        try:
            current_hash = _sha(target.read_bytes())
        except OSError:
            current_hash = None
        return {"success": False,
                "error": f"managed shared update failed; inspect current target and retained journal/backup: {exc}",
                "current_sha256": current_hash,
                "journal": str(journal) if journal is not None else None,
                "backup": str(backup) if backup is not None else None}
    finally:
        # The lock contains only our known marker. Never unlink a replaced lock.
        try:
            if _no_redirect(lock) and lock.read_text(encoding="ascii") == owner_token:
                lock.unlink()
        except OSError:
            pass
