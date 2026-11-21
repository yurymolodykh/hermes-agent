"""Initial profile files, written on the staging tree before publication."""
import contextlib
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def clone_staging_dir(profile_dir: Path) -> Path:
    """Fresh ``profiles/.<name>.staging-<pid>`` beside the final dir (same filesystem, so the publish
    rename is atomic). A leftover from a crashed create is discarded."""
    staging = profile_dir.parent / f".{profile_dir.name}.staging-{os.getpid()}"
    profile_dir.parent.mkdir(parents=True, exist_ok=True)
    if staging.is_symlink() or staging.is_file():
        staging.unlink()
    elif staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
    return staging


def discard_unpublished_vault_key(ref: Optional[dict]) -> None:
    """A create that fails after a --clone-all copy got its own keychain entry (``ref``, returned by
    ``VaultStore.own_copied_key``) deletes exactly that entry; never one inferred from the source."""
    if ref is not None:
        from agent.vault_keychain import delete_key
        delete_key(ref)


#: Root files a clone will edit: a symlink to the source's file is replaced with a private copy.
CLONE_MATERIALIZE = (".env", "config.yaml", "auth.json", "SOUL.md")


def materialize_symlinked_files(profile_dir: Path) -> list[str]:
    """Replace symlinked root files the clone will edit with private copies of their targets (a
    dangling link is dropped). Returns the relative names materialized.

    Lives here rather than in the ``profiles`` facade, which is over its line cap and may only
    shrink; the accessor is a staging-tree concern like the rest of this module.
    """
    done: list[str] = []
    for name in CLONE_MATERIALIZE:
        path = profile_dir / name
        if not path.is_symlink():
            continue
        target = Path(os.path.realpath(path))
        path.unlink()
        if target.is_file():
            shutil.copy2(target, path)
        done.append(name)
    return done


def personal_soul_seed(profile_name: str) -> Optional[str]:
    """Personal SOUL text for a person's own root (``01-<login>``); ``None`` for any other name.

    Delegates to :mod:`hermes_acl_soul`, which owns the transformation and the name rule. Kept out
    of the ``profiles`` facade (which is over its line cap and may only shrink) and best-effort: a
    failure returns ``None`` so the create path falls back to ``DEFAULT_SOUL_MD`` instead of
    failing a profile creation over seeding.
    """
    from hermes_acl_soul import personal_soul_for

    try:
        return personal_soul_for(profile_name)
    except (OSError, ValueError, KeyError):  # unreadable/corrupt source -> bundled base
        logger.warning("personal SOUL seed for %r failed; using the default SOUL",
                       profile_name, exc_info=True)
        return None


def finish_profile_layout(profile_dir: Path, *, no_skills: bool, clone_all: bool,
                           description: Optional[str], soul: Optional[str] = None,
                           personal_root: bool = False) -> None:
    """Seed files a fresh profile owns from day one; runs on the staging tree before publish."""
    from hermes_cli.profiles import (
        _seed_file_if_missing, _PLACEHOLDER_ENV, NO_BUNDLED_SKILLS_MARKER,
        _migrate_profile_config_if_outdated, write_profile_meta,
    )
    if soul is not None:
        (profile_dir / "SOUL.md").write_text(soul, encoding="utf-8")

    # A personal root also carries the questionnaire its person fills in on first entry. Best
    # effort on purpose: the profile is already built by now, and a missing questionnaire must
    # not fail a create — `hermes_acl_soul.seed_questionnaire` can re-place it later. The staging
    # dir is named ``.<name>.staging-<pid>``, so the caller states the namespace instead of this
    # function re-deriving it from the directory name.
    if personal_root:
        with contextlib.suppress(Exception):
            from hermes_acl_soul import seed_questionnaire

            seed_questionnaire(profile_dir)

    # Seed an empty .env so the profile owns a credentials file from day one. Without it,
    # profile-scoped env writes (dashboard Channels/Keys pages, `hermes -p <name> auth add`)
    # had no file until first write and the profile silently inherited shell API keys —
    # read by users as "the new profile reads the root .env". Skipped when a clone copied one.
    _seed_file_if_missing(profile_dir / ".env", _PLACEHOLDER_ENV, 0o600)

    # Default SOUL.md to customize immediately (skipped when a clone already provided one).
    with contextlib.suppress(Exception):  # best-effort — don't fail profile creation over this
        from hermes_cli.default_soul import DEFAULT_SOUL_MD
        _seed_file_if_missing(profile_dir / "SOUL.md", DEFAULT_SOUL_MD)

    # Opt-out marker read by seed_profile_skills() and `hermes update`'s all-profile sync
    # (the feature still works via the empty skills/ dir if this fails).
    if no_skills:
        _seed_file_if_missing(
            profile_dir / NO_BUNDLED_SKILLS_MARKER,
            "This profile opted out of bundled-skill seeding (`hermes profile create --no-skills`).\n"
            "Delete this file to re-enable sync on the next `hermes update`.\n",
        )

    # Migrate config-only clones now so desktop/status don't warn that a just-created
    # profile is v0/outdated; --clone-all snapshots stay byte-for-byte apart from the
    # explicit runtime/history stripping above.
    if not clone_all:
        _migrate_profile_config_if_outdated(profile_dir)

    # Description last, so a partial-create failure doesn't strand a description file.
    if description and description.strip():
        with contextlib.suppress(Exception):  # non-fatal — `hermes profile describe` works later
            write_profile_meta(profile_dir, description=description.strip(), description_auto=False)
