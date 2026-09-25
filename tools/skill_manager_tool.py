#!/usr/bin/env python3
"""
Skill Manager Tool -- Agent-Managed Skill Creation & Editing

Allows the agent to create, update, and delete skills, turning successful
approaches into reusable procedural knowledge. New skills are created in
~/.hermes/skills/. Existing skills (bundled, hub-installed, or user-created)
can be modified or deleted wherever they live.

Skills are the agent's procedural memory: they capture *how to do a specific
type of task* based on proven experience. General memory (MEMORY.md, USER.md) is
broad and declarative. Skills are narrow and actionable.

Actions:
  create     -- Create a new skill (SKILL.md + directory structure)
  edit       -- Replace the SKILL.md content of a user skill (full rewrite)
  patch      -- Targeted find-and-replace within SKILL.md or any supporting file
  delete     -- Remove a user skill entirely
  write_file -- Add/overwrite a supporting file (reference, template, script, asset)
  remove_file-- Remove a supporting file from a user skill

Directory layout for user skills:
    ~/.hermes/skills/
    ├── my-skill/
    │   ├── SKILL.md
    │   ├── references/
    │   ├── templates/
    │   ├── scripts/
    │   └── assets/
    └── category-name/
        └── another-skill/
            └── SKILL.md
"""

import json
import logging
import re
import shutil
import contextvars as _ctxvars
import hashlib
import os
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home, display_hermes_home
from utils import atomic_write_text, is_truthy_value
from hermes_cli.config import cfg_get
from agent.skill_utils import (
    extract_skill_description,
    is_skill_description_truncated_for_prompt,
    parse_frontmatter as _parse_frontmatter,
    SKILL_PROMPT_DESC_LIMIT,
)

logger = logging.getLogger(__name__)

_background_review_read_paths: "_ctxvars.ContextVar[frozenset[str]]" = _ctxvars.ContextVar(
    "background_review_read_paths", default=frozenset()
)


def mark_background_review_skill_read(path: Path) -> None:
    """Record that the active background-review fork has read a skill file.

    The autonomous review fork is allowed to evolve skills, but it must not
    patch or rewrite content it has only inferred from the transcript.  The
    skill_view tool calls this after returning file content to the model; write
    paths below require the corresponding target path to be present when the
    current origin is ``background_review``.
    """
    try:
        from tools.skill_provenance import is_background_review
        if not is_background_review():
            return
    except Exception:
        return

    try:
        resolved = str(path.resolve())
    except Exception:
        resolved = str(path)
    current = set(_background_review_read_paths.get())
    current.add(resolved)
    _background_review_read_paths.set(frozenset(current))


def _background_review_has_read(path: Path) -> bool:
    try:
        resolved = str(path.resolve())
    except Exception:
        resolved = str(path)
    return resolved in _background_review_read_paths.get()


def _reset_background_review_read_marks() -> None:
    """Test helper: clear read-before-write marks for the current context."""
    _background_review_read_paths.set(frozenset())

# Import security scanner — external hub installs always get scanned;
# agent-created skills only get scanned when skills.guard_agent_created is on.
try:
    from tools.skills_guard import scan_skill, should_allow_install, format_scan_report
    _GUARD_AVAILABLE = True
except ImportError:
    _GUARD_AVAILABLE = False


def _guard_agent_created_enabled() -> bool:
    """Read skills.guard_agent_created from config (default False).

    Off by default because the agent can already execute the same code
    paths via terminal() with no gate, so the scan adds friction without
    meaningful security.  Users who want belt-and-suspenders can turn it
    on via `hermes config set skills.guard_agent_created true`.
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return is_truthy_value(
            cfg_get(cfg, "skills", "guard_agent_created"),
            default=False,
        )
    except Exception:
        return False


def _security_scan_skill(skill_dir: Path, *, strict: bool = False) -> Optional[str]:
    """Scan a skill directory after write. Returns error string if blocked, else None.

    No-op when skills.guard_agent_created is disabled (the default).
    Managed shared updates use strict=True so a missing/failing scanner
    cannot turn an enabled guard into a successful publication.
    """
    if not _guard_agent_created_enabled():
        return None
    if not _GUARD_AVAILABLE:
        return "Security scan unavailable" if strict else None
    try:
        result = scan_skill(skill_dir, source="agent-created")
        allowed, reason = should_allow_install(result)
        if allowed is False:
            report = format_scan_report(result)
            return f"Security scan blocked this skill ({reason}):\n{report}"
        if allowed is None:
            # "ask" verdict — for agent-created skills this means dangerous
            # findings were detected.  Surface as an error so the agent can
            # retry with the flagged content removed.
            report = format_scan_report(result)
            logger.warning("Agent-created skill blocked (dangerous findings): %s", reason)
            return f"Security scan blocked this skill ({reason}):\n{report}"
    except Exception as e:
        logger.warning("Security scan failed for %s: %s", skill_dir, e, exc_info=True)
        if strict:
            return "Security scan failed; refusing managed shared update"
    return None

import yaml


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader which rejects duplicate mapping keys instead of silently
    accepting last-writer-wins frontmatter."""


def _unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


# All skills live in ~/.hermes/skills/ (single source of truth)
HERMES_HOME = get_hermes_home()
SKILLS_DIR = HERMES_HOME / "skills"
_SKILLS_DIR_AT_IMPORT = SKILLS_DIR


def _skills_dir() -> Path:
    """Return the active profile's skills directory at call time.

    Long-lived multi-profile runtimes (Dashboard/TUI/Desktop backend, cron,
    kanban workers) import this module once under the launch HERMES_HOME and
    later bind a different profile per session (#40677). Honor an explicitly
    patched module-level ``SKILLS_DIR`` (tests), otherwise resolve from the
    live profile-scoped HERMES_HOME on every call.
    """
    configured = Path(SKILLS_DIR)
    if configured != _SKILLS_DIR_AT_IMPORT:
        return configured
    return get_hermes_home() / "skills"

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024


def _containing_skills_root(skill_path: Path) -> Path:
    """Return the skills root directory (local or external_dirs entry) that
    contains ``skill_path``.  Falls back to the local ``SKILLS_DIR`` if no
    match is found (defensive — callers should have located the skill via
    ``_find_skill`` first).
    """
    from agent.skill_utils import get_all_skills_dirs

    try:
        resolved = skill_path.resolve()
    except OSError:
        resolved = skill_path

    for root in get_all_skills_dirs():
        try:
            resolved.relative_to(root.resolve())
            return root
        except (ValueError, OSError):
            continue
    return _skills_dir()


def _is_path_redirect(path: Path) -> bool:
    """True when ``path`` is a symlink or (on Windows) a directory junction.

    Either form lets a poisoned skills tree redirect a subsequent
    ``shutil.rmtree`` to content outside the skills root. ``is_junction``
    only exists on Python 3.12+ Windows; gate with ``hasattr``.
    """
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return False


def _validate_delete_target(skill_dir: Path) -> Optional[str]:
    """Last-line guard before ``shutil.rmtree(skill_dir)`` in ``_delete_skill``.

    ``_find_skill`` already restricts ``skill_dir`` to a real ``SKILL.md``
    parent discovered by walking the skills roots, so the agent cannot inject
    an arbitrary path the way Kilo Code's HTTP endpoint could (their issue
    #11227: a built-in-skill sentinel resolved to the server cwd and a
    recursive delete wiped the user's entire working directory). This is the
    matching defense-in-depth for our agent-facing ``skill_manage`` delete
    path: even if discovery or a poisoned tree hands us a bad directory, never
    recursively delete

      1. a path that is not strictly *inside* one of the known skills roots,
      2. a skills root itself (would wipe every installed skill), or
      3. a directory reached via a symlink / junction (``rmtree`` would follow
         it into content outside the skills tree).

    Returns an error string to refuse on, or ``None`` when the delete is safe.
    """
    from agent.skill_utils import get_all_skills_dirs

    # (3) Reject symlink/junction redirects on the skill directory itself.
    if _is_path_redirect(skill_dir):
        return (
            f"Refusing to delete '{skill_dir}': the skill directory is a "
            f"symlink/junction. Remove the link target manually if intended."
        )

    try:
        resolved = skill_dir.resolve()
    except OSError as exc:
        return f"Refusing to delete '{skill_dir}': could not resolve path ({exc})."

    roots = []
    for root in get_all_skills_dirs():
        try:
            roots.append(root.resolve())
        except OSError:
            continue

    for root in roots:
        # (2) Never rmtree a skills root itself.
        if resolved == root:
            return (
                f"Refusing to delete '{skill_dir}': resolves to the skills root "
                f"itself, which would remove every installed skill."
            )
        # (1) Must be strictly inside a known root.
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if rel.parts:  # at least one component below the root
            return None

    return (
        f"Refusing to delete '{skill_dir}': path does not resolve inside any "
        f"known skills root."
    )


def _pinned_guard(name: str) -> Optional[str]:
    """Return a refusal message if *name* is pinned, else None.

    Pin protects a skill from **deletion** — both the curator's auto-archive
    passes and the agent's ``skill_manage(action="delete")`` tool call. The
    agent can still patch/edit pinned skills; pin only guards against
    irrecoverable loss, not against content evolution.

    Best-effort: if the sidecar is unreadable we let the delete through
    rather than block on a broken telemetry file.
    """
    try:
        from tools import skill_usage
        rec = skill_usage.get_record(name)
        if rec.get("pinned"):
            return (
                f"Skill '{name}' is pinned and cannot be deleted by "
                f"skill_manage. Ask the user to run "
                f"`hermes curator unpin {name}` if they want to delete it. "
                f"Patches and edits are allowed on pinned skills; only "
                f"deletion is blocked."
            )
    except Exception:
        logger.debug("pinned-guard lookup failed for %s", name, exc_info=True)
    return None


def _background_review_write_guard(
    name: str,
    skill_dir: Path,
    action: str,
) -> Optional[Dict[str, Any]]:
    """Refuse autonomous curator writes to externally owned skills.

    Foreground agents may still perform user-directed edits to external,
    bundled, or hub-installed skills. The background review fork is different:
    it is autonomous lifecycle maintenance, so its write surface is restricted
    to local curator-owned sediment.
    """
    try:
        from tools.skill_provenance import is_background_review
        if not is_background_review():
            return None
    except Exception:
        return None

    # Pin must be respected by autonomous maintenance. The curator already
    # skips pinned skills from every auto-transition; the background review
    # fork is the same kind of autonomous, no-user-present actor, so it must
    # not write to a pinned skill either (issue #25839). This is stricter than
    # the foreground ``_pinned_guard`` (which only blocks deletion) precisely
    # because there is no user in the loop to consent to an edit here.
    try:
        from tools import skill_usage
        if skill_usage.get_record(name).get("pinned"):
            return {
                "success": False,
                "error": (
                    f"Refusing background curator {action} for pinned skill "
                    f"'{name}': pinned skills are off-limits to autonomous "
                    "maintenance. Ask the user to run "
                    f"`hermes curator unpin {name}` if they want it changed."
                ),
            }
    except Exception:
        logger.debug("pinned skill guard lookup failed for %s", name, exc_info=True)

    try:
        from agent.skill_utils import is_external_skill_path
        if is_external_skill_path(skill_dir):
            return {
                "success": False,
                "error": (
                    f"Refusing background curator {action} for skill '{name}': "
                    "the skill lives in skills.external_dirs, which are "
                    "externally owned and read-only to autonomous curation."
                ),
            }
    except Exception:
        logger.debug("external skill guard lookup failed for %s", name, exc_info=True)

    try:
        from tools import skill_usage
        if skill_usage.is_protected_builtin(name):
            return {
                "success": False,
                "error": (
                    f"Refusing background curator {action} for protected "
                    f"built-in skill '{name}'."
                ),
            }
        if skill_usage.is_hub_installed(name):
            return {
                "success": False,
                "error": (
                    f"Refusing background curator {action} for hub-installed "
                    f"skill '{name}'."
                ),
            }
        if skill_usage.is_bundled(name):
            return {
                "success": False,
                "error": (
                    f"Refusing background curator {action} for bundled "
                    f"skill '{name}'."
                ),
            }
        # Skills that are not curator-managed are off-limits to autonomous
        # curation. This prevents the LLM consolidation pass from mutating
        # skills the user owns (manually authored, URL-installed, or created by
        # a foreground `skill_manage(create)` at the user's request), which lack
        # the `created_by: "agent"` marker.
        #
        # A MISSING record and an explicit `created_by: null` must resolve
        # IDENTICALLY (issue #67140). Keying on `isinstance(usage_rec, dict)`
        # made the policy depend on the guard's own side effect: a local skill
        # with no telemetry record passed, the successful write called
        # bump_patch() which created a `created_by: null` record, and the very
        # same write was refused from then on. "Allowed exactly once" is not a
        # policy — it is a race with our own bookkeeping. Fail closed for both
        # shapes; `hermes curator adopt <name>` is the supported way in.
        usage_data = skill_usage.load_usage()
        usage_rec = usage_data.get(name)
        if not skill_usage._is_curator_managed_record(usage_rec):
            if isinstance(usage_rec, dict):
                _detail = f"created_by={usage_rec.get('created_by')!r}"
            else:
                _detail = "no usage record"
            return {
                "success": False,
                "error": (
                    f"Refusing background curator {action} for skill "
                    f"'{name}': the skill is not curator-managed ({_detail}). "
                    "User-owned skills are off-limits to autonomous curation. "
                    f"Run `hermes curator adopt {name}` to opt it in."
                ),
            }
    except Exception:
        logger.warning("owned skill guard lookup failed for %s", name, exc_info=True)
        return {
            "success": False,
            "error": (
                f"Refusing background curator {action} for skill '{name}': "
                "agent ownership could not be verified because the provenance "
                "record is unavailable or unreadable."
            ),
        }
    return None


def _background_review_read_before_write_guard(
    name: str,
    target: Path,
    action: str,
    file_label: str,
) -> Optional[Dict[str, Any]]:
    """Require review forks to load the exact target before mutating it."""
    try:
        from tools.skill_provenance import is_background_review
        if not is_background_review():
            return None
    except Exception:
        return None

    if _background_review_has_read(target):
        return None

    return {
        "success": False,
        "error": (
            f"Refusing background curator {action} for skill '{name}': "
            f"the current {file_label} content has not been loaded in this "
            "review turn. Call skill_view(name) for SKILL.md, or "
            "skill_view(name, file_path=...) for a supporting file, then "
            "retry the write using the content just returned."
        ),
        "_read_before_write_required": True,
    }


def _background_review_preflight(action: str, name: str) -> Optional[Dict[str, Any]]:
    if action not in {"edit", "patch", "delete", "write_file", "remove_file"}:
        return None
    existing = _find_skill(name)
    if not existing:
        return None
    return _background_review_write_guard(name, existing["path"], action)


def _curator_consolidation_delete_guard(
    name: str, absorbed_into: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Fail closed on unverified deletes during the curator consolidation pass.

    The curator's forked review agent (``is_background_review()``) runs the
    LLM umbrella-building pass. Its only legitimate ``skill_manage(delete)`` is
    a *verified consolidation*: the skill's content was absorbed into an
    umbrella, declared via ``absorbed_into=<umbrella>`` where the umbrella
    exists on disk (validated separately in ``_delete_skill``).

    A delete with no forwarding target — ``absorbed_into`` omitted (``None``)
    or empty (``""``) — is the fail-open behavior reported in #29912: the
    consolidation pass archived whole clusters of active skills with zero
    verified consolidations (``consolidated_this_run == 0``), leaving active
    automations pointing at names that no longer resolve. The deterministic
    inactivity prune is the only legitimate prune path, and it archives via
    ``skill_usage.archive_skill()`` directly without ever calling
    ``skill_manage`` — so a bare prune reaching here can only be the LLM pass
    pruning without consolidation evidence. Refuse it; keep the skill active.

    Returns an error dict to abort the delete, or ``None`` when the delete is
    allowed to proceed (not the curator pass, or a declared consolidation).
    """
    try:
        from tools.skill_provenance import is_background_review
        if not is_background_review():
            return None
    except Exception:
        return None

    declared = isinstance(absorbed_into, str) and absorbed_into.strip()
    if declared:
        return None

    return {
        "success": False,
        "error": (
            f"Refusing background curator delete of skill '{name}': the "
            "consolidation pass may only archive a skill it has absorbed into "
            "an umbrella. Pass absorbed_into=<umbrella> (the umbrella must "
            "already exist) to record a verified consolidation. Pruning a "
            "skill with no forwarding target is not permitted here — the "
            "deterministic inactivity prune handles staleness archival "
            "separately. Keeping '{name}' active.".format(name=name)
        ),
        "_fail_closed": True,
    }


MAX_SKILL_CONTENT_CHARS = 100_000   # ~36k tokens at 2.75 chars/token
MAX_SKILL_FILE_BYTES = 1_048_576    # 1 MiB per supporting file

# Characters allowed in skill names (filesystem-safe, URL-friendly)
VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')

# Subdirectories allowed for write_file/remove_file
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}


# =============================================================================
# Validation helpers
# =============================================================================

def _validate_name(name: str) -> Optional[str]:
    """Validate a skill name. Returns error message or None if valid."""
    if not name:
        return "Skill name is required."
    if len(name) > MAX_NAME_LENGTH:
        return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
            f"hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return None


def _validate_category(category: Optional[str]) -> Optional[str]:
    """Validate an optional category name used as a single directory segment."""
    if category is None:
        return None
    if not isinstance(category, str):
        return "Category must be a string."

    category = category.strip()
    if not category:
        return None
    if "/" in category or "\\" in category:
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    if len(category) > MAX_NAME_LENGTH:
        return f"Category exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(category):
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    return None


def _validate_frontmatter(content: str, *, new_skill: bool = False) -> Optional[str]:
    """
    Validate that SKILL.md content has proper frontmatter with required fields.
    Returns error message or None if valid.

    When ``new_skill`` is True (create path only), the description must also
    fit the 60-char system-prompt budget (SKILL_PROMPT_DESC_LIMIT) so newly
    authored skills never lose routing signal to index truncation. Edit and
    patch paths deliberately skip this so existing over-limit skills remain
    maintainable while their descriptions are cleaned up.
    """
    if not content.strip():
        return "Content cannot be empty."

    # Tolerate a leading UTF-8 BOM (Windows editors) before the fence.
    content = content.lstrip("\ufeff")

    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."

    end_match = re.search(r'\n---\s*\n', content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."

    yaml_content = content[3:end_match.start() + 3]

    try:
        parsed = yaml.load(yaml_content, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as e:
        return f"YAML frontmatter parse error: {e}"

    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."

    if "name" not in parsed:
        return "Frontmatter must include 'name' field."
    if "description" not in parsed:
        return "Frontmatter must include 'description' field."
    desc = str(parsed["description"])
    if len(desc) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."
    if new_skill and len(desc.strip().strip("'\"")) > SKILL_PROMPT_DESC_LIMIT:
        return (
            f"Description is {len(desc.strip())} chars — new skills must fit the "
            f"{SKILL_PROMPT_DESC_LIMIT}-char system-prompt budget (one sentence, "
            f"trigger first, ends with a period). The skill index truncates "
            f"longer descriptions to {SKILL_PROMPT_DESC_LIMIT - 3} chars + '...', "
            f"destroying the routing signal. Move detail into the skill body."
        )

    body = content[end_match.end() + 3:].strip()
    if not body:
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."

    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> Optional[str]:
    """Check that content doesn't exceed the character limit for agent writes.

    Returns an error message or None if within bounds.
    """
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters "
            f"(limit: {MAX_SKILL_CONTENT_CHARS:,}). "
            f"Consider splitting into a smaller SKILL.md with supporting files "
            f"in references/ or templates/."
        )
    return None


def _resolve_skill_dir(name: str, category: str = None) -> Path:
    """Build the directory path for a new skill, optionally under a category."""
    if category:
        return _skills_dir() / category / name
    return _skills_dir() / name


def _find_skill(name: str) -> Optional[Dict[str, Any]]:
    """
    Find a skill by name across all skill directories.

    Searches the local skills dir (~/.hermes/skills/) first, then any
    external dirs configured via skills.external_dirs.  Returns
    {"path": Path} or None.
    """
    from agent.skill_utils import get_all_skills_dirs, is_excluded_skill_path
    for skills_dir in get_all_skills_dirs():
        if not skills_dir.exists():
            continue
        for skill_md in skills_dir.rglob("SKILL.md"):
            if is_excluded_skill_path(skill_md):
                continue
            if skill_md.parent.name == name:
                return {"path": skill_md.parent}
    return None


def _maybe_auto_propose_org_edit(name: str, skill_path: Path) -> Optional[str]:
    """Submit an org-skill edit upstream when `sync.org_auto_propose` is on.

    Returns a short note for the tool result, or None when nothing happened.
    Never raises: an offline/failed submission must not fail the edit itself —
    the change is already saved locally and can be proposed later.
    """
    try:
        from agent.skill_utils import is_org_mirror_path
        from tools import skills_sync_client as ssc

        if not is_org_mirror_path(skill_path, _skills_dir()):
            return None
        if not ssc.sync_org_auto_propose():
            return (
                f"This skill is shared by your organisation. Your edit is "
                f"saved locally and will not be overwritten by org updates. "
                f"Run `hermes sync propose {name}` to share it back."
            )
        result = ssc.propose_skill(name)
        if result.get("proposal_pending"):
            return (
                f"Auto-proposed to your organisation as proposal "
                f"#{result.get('proposal_id')} (pending admin review)."
            )
        return "Auto-proposed to your organisation (merged into the shared set)."
    except Exception as e:
        logger.debug("auto-propose skipped for %s: %s", name, e)
        return (
            f"Edit saved locally. Could not submit it to your organisation "
            f"right now — run `hermes sync propose {name}` to retry."
        )


def _org_mirror_write_guard(name: str, skill_path: Path, action: str) -> Optional[Dict[str, Any]]:
    """Org-shared skills are EDITABLE IN PLACE — this only blocks deletion.

    Earlier versions refused every write to `_org/`, which broke the learning
    loop exactly where it matters most: the agent is told to patch a skill the
    moment it finds a gap, and shared skills are the ones the most people use.
    Blocking that froze org skills while personal ones kept improving, and the
    "fork it into a personal skill" alternative is not something an agent does
    mid-task — so improvements were simply lost.

    Now an edit lands in the mirror and is protected from being overwritten by
    the next org pull (see the baseline sidecar in skills_sync_client). It
    reaches the organisation when the user runs `hermes sync propose`, or
    immediately if `sync.org_auto_propose` is on.

    Deletion is still refused: the mirror is a materialized view of the org
    HEAD, so a local delete is meaningless (the next pull restores it) and
    removing a skill for the organisation is an admin action, not a local one.
    """
    if action not in {"delete", "remove_file"}:
        return None
    try:
        from agent.skill_utils import is_org_mirror_path

        if is_org_mirror_path(skill_path, _skills_dir()):
            return {
                "success": False,
                "error": (
                    f"Cannot {action} '{name}' locally: it is shared by your "
                    "organisation, so a local delete would just come back on "
                    "the next sync. Ask an org admin to remove it for "
                    "everyone. (Editing it IS allowed — your changes are kept "
                    "and can be proposed back with `hermes sync propose "
                    f"{name}`.)"
                ),
            }
    except Exception:
        logger.debug("org mirror guard lookup failed for %s", name, exc_info=True)
    return None


def _find_skill_in_other_profiles(name: str) -> List[Tuple[str, Path]]:
    """Look for ``name`` under SKILL.md across OTHER Hermes profiles.

    Returns a list of ``(profile_name, skill_dir)`` pairs. Used to make
    the "Skill X not found" error explain when the user is editing the
    wrong profile. Empty list when no other profile has the skill (or
    when profile discovery fails — fail-quiet, the caller falls back to
    the plain "not found" error).
    """
    matches: List[Tuple[str, Path]] = []
    try:
        from hermes_constants import get_default_hermes_root
        from agent.skill_utils import is_excluded_skill_path
    except Exception:
        return matches

    try:
        root = get_default_hermes_root()
    except Exception:
        return matches

    # Collect (profile_name, skills_dir) for every profile EXCEPT the
    # one whose skills dir we already searched in _find_skill().
    _active = _skills_dir()
    active_dir = _active.resolve() if _active.exists() else _active
    candidates: List[Tuple[str, Path]] = []

    # Default profile (~/.hermes/skills) — only consider when active is non-default.
    default_skills = root / "skills"
    try:
        if default_skills.resolve() != active_dir:
            candidates.append(("default", default_skills))
    except (OSError, RuntimeError):
        pass

    # All named profiles (~/.hermes/profiles/*/skills)
    profiles_root = root / "profiles"
    if profiles_root.is_dir():
        try:
            for entry in profiles_root.iterdir():
                if not entry.is_dir():
                    continue
                pskills = entry / "skills"
                try:
                    if pskills.resolve() == active_dir:
                        continue
                except (OSError, RuntimeError):
                    continue
                candidates.append((entry.name, pskills))
        except OSError:
            pass

    for profile_name, skills_dir in candidates:
        if not skills_dir.is_dir():
            continue
        try:
            for skill_md in skills_dir.rglob("SKILL.md"):
                if is_excluded_skill_path(skill_md):
                    continue
                if skill_md.parent.name == name:
                    matches.append((profile_name, skill_md.parent))
                    break  # one match per profile is enough
        except OSError:
            continue
    return matches


def _skill_not_found_error(name: str, suffix: str = "") -> str:
    """Build a "skill not found" error that names other profiles holding
    the same skill, so the agent can recognize a profile-scoping mistake.

    ``suffix`` is appended after the cross-profile hint if present
    (e.g. ``" Create it first with action='create'."``).
    """
    from agent.file_safety import _resolve_active_profile_name
    active = _resolve_active_profile_name()
    base = f"Skill '{name}' not found in active profile '{active}'."

    others = _find_skill_in_other_profiles(name)
    if others:
        if len(others) == 1:
            other_profile, other_path = others[0]
            base += (
                f" A skill by that name exists in profile "
                f"'{other_profile}' ({other_path}). To edit a skill in "
                f"another profile, switch profiles (`hermes -p "
                f"{other_profile}`) or operate via explicit file tools "
                f"with ``cross_profile=True``."
            )
        else:
            names = ", ".join(f"'{p}'" for p, _ in others)
            base += (
                f" Skills by that name exist in other profiles: {names}. "
                f"Switch profiles (`hermes -p <name>`) to edit there, or "
                f"operate via explicit file tools with ``cross_profile=True``."
            )
    else:
        base += " Use skills_list() to see available skills."

    if suffix:
        base += suffix
    return base


def _validate_file_path(file_path: str) -> Optional[str]:
    """
    Validate a file path for write_file/remove_file.
    Must be under an allowed subdirectory and not escape the skill dir.
    """
    from tools.path_security import has_traversal_component

    if not file_path:
        return "file_path is required."

    normalized = Path(file_path)

    # Prevent path traversal (checked before any allow-listing so the SKILL.md
    # exception below can never be reached by a traversal-laden path).
    if has_traversal_component(file_path):
        return "Path traversal ('..') is not allowed."

    # SKILL.md is the canonical skill file and lives at the skill root, not
    # under an allowed subdirectory. Accept its two natural spellings —
    # 'SKILL.md' and '<skill-name>/SKILL.md' — so callers can target the main
    # file. The traversal guard above still applies, so this can't escape.
    if normalized.parts and normalized.name == "SKILL.md":
        if len(normalized.parts) == 1 or len(normalized.parts) == 2:
            return None

    # Must be under an allowed subdirectory
    if not normalized.parts or normalized.parts[0] not in ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(ALLOWED_SUBDIRS))
        return f"File must be under one of: {allowed}. Got: '{file_path}'"

    # Must have a filename (not just a directory)
    if len(normalized.parts) < 2:
        return f"Provide a file path, not just a directory. Example: '{normalized.parts[0]}/myfile.md'"

    return None


def _resolve_skill_target(skill_dir: Path, file_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a supporting-file path and ensure it stays within the skill directory."""
    from tools.path_security import validate_within_dir

    target = skill_dir / file_path
    error = validate_within_dir(target, skill_dir)
    if error:
        return None, error
    return target, None


# =============================================================================
# Core actions
# =============================================================================


def _add_description_prompt_preview(result: Dict[str, Any], content: str) -> None:
    """Append a system_prompt_preview field when the description will be truncated."""
    fm, _ = _parse_frontmatter(content)
    if is_skill_description_truncated_for_prompt(fm):
        result["system_prompt_preview"] = (
            f"System prompt will show: \"{extract_skill_description(fm)}\" — "
            f"keep the trigger self-contained in the first "
            f"{SKILL_PROMPT_DESC_LIMIT - 3} chars."
        )


def _create_skill(name: str, content: str, category: str = None) -> Dict[str, Any]:
    """Create a new user skill with SKILL.md content."""
    # Validate name
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}

    err = _validate_category(category)
    if err:
        return {"success": False, "error": err}

    # Validate content
    err = _validate_frontmatter(content, new_skill=True)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    # Check for name collisions across all directories
    existing = _find_skill(name)
    if existing:
        return {
            "success": False,
            "error": f"A skill named '{name}' already exists at {existing['path']}."
        }

    # Create the skill directory
    skill_dir = _resolve_skill_dir(name, category)
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Write instructional documents with a readable mode while preserving
    # the mode of an existing file across the atomic replacement.
    skill_md = skill_dir / "SKILL.md"
    atomic_write_text(skill_md, content, preserve_mode=True, create_mode=0o644)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(skill_dir)
    if scan_error:
        shutil.rmtree(skill_dir, ignore_errors=True)
        return {"success": False, "error": scan_error}

    # Extract description from frontmatter for verbose notifications
    _desc = ""
    try:
        _fm_end = re.search(r'\n---\s*\n', content[3:])
        if _fm_end:
            _parsed = yaml.safe_load(content[3:_fm_end.start() + 3])
            _desc = str(_parsed.get("description", ""))[:120]
    except Exception:
        pass

    result = {
        "success": True,
        "message": f"Skill '{name}' created.",
        "path": str(skill_dir.relative_to(_skills_dir())),
        "skill_md": str(skill_md),
        "_change": {"description": _desc},
    }
    if category:
        result["category"] = category
    result["hint"] = (
        "To add reference files, templates, or scripts, use "
        "skill_manage(action='write_file', name='{}', file_path='references/example.md', file_content='...')".format(name)
    )
    _add_description_prompt_preview(result, content)
    _attach_lint_findings(result, skill_md)
    return result


def _attach_lint_findings(result: Dict[str, Any], skill_md: Path) -> None:
    """Run the advisory SKILL.md linter and attach any findings to *result*.

    The linter enforces the CONTRIBUTING "Skill authoring standards (HARDLINE)"
    conventions that the hard validator does not (shell-utility references,
    missing metadata, dangling reference links, POSIX gating, forbidden files).
    Findings are ADVISORY — surfaced as guidance so the author can fix them,
    never a hard block. The hard rejects already ran in _validate_frontmatter.
    """
    try:
        from tools.skill_linter import lint_skill  # local import: optional path

        findings = lint_skill(skill_md)
    except Exception:
        return
    if not findings:
        return
    result["lint_warnings"] = [
        {"severity": f.severity, "rule": f.rule, "message": f.message}
        for f in findings
    ]
    result["lint_hint"] = (
        "The skill was created. These are advisory authoring-convention "
        "findings (not blockers) — fix them with skill_manage(action='patch') "
        "to match Hermes skill standards."
    )


def _edit_skill(name: str, content: str) -> Dict[str, Any]:
    """Replace the SKILL.md of any existing skill (full rewrite)."""
    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}
    org_guard = _org_mirror_write_guard(name, existing["path"], "edit")
    if org_guard:
        return org_guard
    guard = _background_review_write_guard(name, existing["path"], "edit")
    if guard:
        return guard

    skill_md = existing["path"] / "SKILL.md"
    read_guard = _background_review_read_before_write_guard(
        name, skill_md, "edit", "SKILL.md"
    )
    if read_guard:
        return read_guard

    # Back up original content for rollback
    original_content = skill_md.read_text(encoding="utf-8") if skill_md.exists() else None
    atomic_write_text(skill_md, content, preserve_mode=True, create_mode=0o644)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(existing["path"])
    if scan_error:
        if original_content is not None:
            atomic_write_text(skill_md, original_content, preserve_mode=True)
        return {"success": False, "error": scan_error}

    # Extract description from new content for verbose notifications
    _desc = ""
    try:
        _fm_end = re.search(r'\n---\s*\n', content[3:])
        if _fm_end:
            _parsed = yaml.safe_load(content[3:_fm_end.start() + 3])
            _desc = str(_parsed.get("description", ""))[:120]
    except Exception:
        pass

    result = {
        "success": True,
        "message": f"Skill '{name}' updated (full rewrite).",
        "path": str(existing["path"]),
        "_change": {"description": _desc},
    }
    org_note = _maybe_auto_propose_org_edit(name, existing["path"])
    if org_note:
        result["org_sharing"] = org_note
        result["message"] = f"{result['message']} {org_note}"
    _add_description_prompt_preview(result, content)
    return result


def _patch_skill(
    name: str,
    old_string: str,
    new_string: str,
    file_path: str = None,
    replace_all: bool = False,
) -> Dict[str, Any]:
    """Targeted find-and-replace within a skill file.

    Defaults to SKILL.md. Use file_path to patch a supporting file instead.
    Requires a unique match unless replace_all is True.
    """
    if not old_string:
        return {"success": False, "error": "old_string is required for 'patch'."}
    if new_string is None:
        return {"success": False, "error": "new_string is required for 'patch'. Use an empty string to delete matched text."}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}

    skill_dir = existing["path"]
    org_guard = _org_mirror_write_guard(name, skill_dir, "patch")
    if org_guard:
        return org_guard
    guard = _background_review_write_guard(name, skill_dir, "patch")
    if guard:
        return guard

    if file_path:
        # Patching a supporting file
        err = _validate_file_path(file_path)
        if err:
            return {"success": False, "error": err}
        target, err = _resolve_skill_target(skill_dir, file_path)
        if err:
            return {"success": False, "error": err}
        assert target is not None
    else:
        # Patching SKILL.md
        target = skill_dir / "SKILL.md"

    if not target.exists():
        return {"success": False, "error": f"File not found: {target.relative_to(skill_dir)}"}

    read_guard = _background_review_read_before_write_guard(
        name,
        target,
        "patch",
        "SKILL.md" if not file_path else file_path,
    )
    if read_guard:
        return read_guard

    content = target.read_text(encoding="utf-8")

    # Use the same fuzzy matching engine as the file patch tool.
    # This handles whitespace normalization, indentation differences,
    # escape sequences, and block-anchor matching — saving the agent
    # from exact-match failures on minor formatting mismatches.
    from tools.fuzzy_match import fuzzy_find_and_replace

    new_content, match_count, _strategy, match_error = fuzzy_find_and_replace(
        content, old_string, new_string, replace_all
    )
    if match_error:
        # Show a short preview of the file so the model can self-correct
        preview = content[:500] + ("..." if len(content) > 500 else "")
        err_msg = match_error
        try:
            from tools.fuzzy_match import format_no_match_hint
            err_msg += format_no_match_hint(match_error, match_count, old_string, content)
        except Exception:
            pass
        return {
            "success": False,
            "error": err_msg,
            "file_preview": preview,
        }

    # Check size limit on the result
    target_label = "SKILL.md" if not file_path else file_path
    err = _validate_content_size(new_content, label=target_label)
    if err:
        return {"success": False, "error": err}

    # If patching SKILL.md, validate frontmatter is still intact
    if not file_path:
        err = _validate_frontmatter(new_content)
        if err:
            return {
                "success": False,
                "error": f"Patch would break SKILL.md structure: {err}",
            }

    original_content = content  # for rollback
    atomic_write_text(target, new_content, preserve_mode=True, create_mode=0o644)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(skill_dir)
    if scan_error:
        atomic_write_text(target, original_content, preserve_mode=True)
        return {"success": False, "error": scan_error}

    result = {
        "success": True,
        "message": f"Patched {'SKILL.md' if not file_path else file_path} in skill '{name}' ({match_count} replacement{'s' if match_count > 1 else ''}).",
    }
    # Include change previews for verbose notifications
    result["_change"] = {
        "old": old_string[:200] + ("…" if len(old_string) > 200 else ""),
        "new": new_string[:200] + ("…" if len(new_string) > 200 else ""),
    }
    org_note = _maybe_auto_propose_org_edit(name, skill_dir)
    if org_note:
        result["org_sharing"] = org_note
        result["message"] = f"{result['message']} {org_note}"
    return result


def _external_destructive_write_guard(name: str, skill_dir: Path, action: str) -> Optional[Dict[str, Any]]:
    """Do not turn an external loader root into a recursive/unlink write API.

    This is an exact-path ownership check, shared by skill_manage's preflight
    and the low-level helpers. Only update_shared has the narrow CAS/backup
    authorization for a configured shared SKILL.md; it never deletes files.
    """
    try:
        skill_dir.resolve().relative_to(_skills_dir().resolve())
    except (OSError, ValueError, RuntimeError):
        return {"success": False, "error":
                f"Refusing {action} for read-only external/shared skill '{name}'; "
                "destructive skill_manage actions are confined to the profile-local skills root."}
    return None


def _delete_skill(name: str, absorbed_into: Optional[str] = None) -> Dict[str, Any]:
    """Delete a profile-local skill.

    ``absorbed_into`` declares intent:
      - ``None`` / missing  → caller didn't declare (legacy / non-curator path);
        accepted for backward compat but logs a warning because the curator
        classification pipeline can't tell consolidation from pruning without it.
      - ``""`` (empty)      → explicit "truly pruned, no forwarding target".
      - ``"<skill-name>"``  → content was absorbed into that umbrella; the
        target must exist on disk. Validated here so the model can't claim an
        umbrella that doesn't exist.
    """
    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}
    readonly_guard = _external_destructive_write_guard(name, existing["path"], "delete")
    if readonly_guard:
        return readonly_guard
    org_guard = _org_mirror_write_guard(name, existing["path"], "delete")
    if org_guard:
        return org_guard
    guard = _background_review_write_guard(name, existing["path"], "delete")
    if guard:
        return guard

    # Fail closed on unverified deletes during the curator consolidation pass.
    # A bare prune (no absorbed_into) from the LLM umbrella pass is the
    # fail-open behavior reported in #29912 — refuse it; keep the skill active.
    fail_closed = _curator_consolidation_delete_guard(name, absorbed_into)
    if fail_closed:
        return fail_closed

    pinned_err = _pinned_guard(name)
    if pinned_err:
        return {"success": False, "error": pinned_err}

    # Validate absorbed_into target when declared non-empty
    absorbed_target = (
        absorbed_into.strip()
        if absorbed_into is not None and isinstance(absorbed_into, str)
        else ""
    )
    is_consolidation = bool(absorbed_target)
    if is_consolidation:
        target_name = absorbed_target
        if target_name == name:
            return {
                "success": False,
                "error": f"absorbed_into='{target_name}' cannot equal the skill being deleted.",
            }
        target = _find_skill(target_name)
        if not target:
            return {
                "success": False,
                "error": (
                    f"absorbed_into='{target_name}' does not exist. "
                    f"Create or patch the umbrella skill first, then retry the delete."
                ),
            }

    skill_dir = existing["path"]
    skills_root = _containing_skills_root(skill_dir)

    # Defense-in-depth before the recursive delete (port of Kilo Code #11240).
    unsafe = _validate_delete_target(skill_dir)
    if unsafe:
        return {"success": False, "error": unsafe}

    # During the curator consolidation pass, a verified consolidation must be
    # RECOVERABLE: archival into ~/.hermes/skills/.archive/ is documented as
    # the maximum destructive action the curator may take, and
    # `hermes curator restore` promises the skill can be brought back. Route
    # through the recoverable archive primitive instead of permanent rmtree so
    # a misjudged consolidation can be undone (#29912). Foreground,
    # user-directed deletes keep their existing hard-delete semantics.
    try:
        from tools.skill_provenance import is_background_review
        curator_pass = is_background_review()
    except Exception:
        curator_pass = False

    if curator_pass:
        try:
            from tools.skill_usage import archive_skill
            ok, archive_msg = archive_skill(name)
        except Exception as e:
            return {"success": False, "error": f"failed to archive '{name}': {e}"}
        if not ok:
            return {"success": False, "error": archive_msg}
        message = f"Skill '{name}' archived ({archive_msg})."
        if is_consolidation:
            message += f" Content absorbed into '{absorbed_target}'."
        return {"success": True, "message": message, "_archived": True}

    shutil.rmtree(skill_dir)

    # Clean up empty category directories (don't remove the skills root itself)
    parent = skill_dir.parent
    if parent != skills_root and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    message = f"Skill '{name}' deleted."
    if is_consolidation:
        message += f" Content absorbed into '{absorbed_target}'."

    return {
        "success": True,
        "message": message,
    }


def _write_file(name: str, file_path: str, file_content: str) -> Dict[str, Any]:
    """Add or overwrite a supporting file within any skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    if not file_content and file_content != "":
        return {"success": False, "error": "file_content is required."}

    # Check size limits
    content_bytes = len(file_content.encode("utf-8"))
    if content_bytes > MAX_SKILL_FILE_BYTES:
        return {
            "success": False,
            "error": (
                f"File content is {content_bytes:,} bytes "
                f"(limit: {MAX_SKILL_FILE_BYTES:,} bytes / 1 MiB). "
                f"Consider splitting into smaller files."
            ),
        }
    err = _validate_content_size(file_content, label=file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name, " Create it first with action='create'.")}
    org_guard = _org_mirror_write_guard(name, existing["path"], "write_file")
    if org_guard:
        return org_guard
    guard = _background_review_write_guard(name, existing["path"], "write_file")
    if guard:
        return guard

    target, err = _resolve_skill_target(existing["path"], file_path)
    if err:
        return {"success": False, "error": err}
    assert target is not None
    if target.exists():
        read_guard = _background_review_read_before_write_guard(
            name, target, "write_file", file_path
        )
        if read_guard:
            return read_guard
    target.parent.mkdir(parents=True, exist_ok=True)
    # Back up for rollback
    original_content = target.read_text(encoding="utf-8") if target.exists() else None
    atomic_write_text(target, file_content, preserve_mode=True, create_mode=0o644)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(existing["path"])
    if scan_error:
        if original_content is not None:
            atomic_write_text(target, original_content, preserve_mode=True)
        else:
            target.unlink(missing_ok=True)
        return {"success": False, "error": scan_error}

    result = {
        "success": True,
        "message": f"File '{file_path}' written to skill '{name}'.",
        "path": str(target),
    }
    org_note = _maybe_auto_propose_org_edit(name, existing["path"])
    if org_note:
        result["org_sharing"] = org_note
        result["message"] = f"{result['message']} {org_note}"
    return result


def _remove_file(name: str, file_path: str) -> Dict[str, Any]:
    """Remove a supporting file from any skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}

    skill_dir = existing["path"]
    readonly_guard = _external_destructive_write_guard(name, skill_dir, "remove_file")
    if readonly_guard:
        return readonly_guard
    guard = _background_review_write_guard(name, skill_dir, "remove_file")
    if guard:
        return guard

    target, err = _resolve_skill_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    assert target is not None
    if not target.exists():
        # List what's actually there for the model to see
        available = []
        for subdir in ALLOWED_SUBDIRS:
            d = skill_dir / subdir
            if d.exists():
                for f in d.rglob("*"):
                    if f.is_file():
                        available.append(str(f.relative_to(skill_dir)))
        return {
            "success": False,
            "error": f"File '{file_path}' not found in skill '{name}'.",
            "available_files": available if available else None,
        }

    read_guard = _background_review_read_before_write_guard(
        name, target, "remove_file", file_path
    )
    if read_guard:
        return read_guard

    target.unlink()

    # Clean up empty subdirectories
    parent = target.parent
    if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    return {
        "success": True,
        "message": f"File '{file_path}' removed from skill '{name}'.",
    }


# -----------------------------------------------------------------------------
# Shared-skill promotion
# -----------------------------------------------------------------------------

_PROMOTION_ALLOWED_DIRS = {"references", "templates", "scripts", "assets"}
_PROMOTION_EXCLUDED_NAMES = {"__pycache__", "smoke-output"}
_PROMOTION_EXCLUDED_PREFIXES = ("r3-", "task-", "run-")


def _promotion_excluded(relative: Path) -> bool:
    """Return whether a source path is generated/runtime residue, not package data."""
    return any(
        part in _PROMOTION_EXCLUDED_NAMES or part.startswith(_PROMOTION_EXCLUDED_PREFIXES)
        for part in relative.parts
    )


def _promotion_safe_relative(path: Path, root: Path) -> Optional[Path]:
    """Return a non-escaping relative path, rejecting links and junctions."""
    if _is_path_redirect(path):
        return None
    try:
        resolved_root = root.resolve()
        resolved = path.resolve()
        relative = resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    return relative


def _promotion_files(source_dir: Path, local_root: Path) -> Tuple[Optional[List[Tuple[Path, Path]]], Optional[str]]:
    """Collect the redistributable package without following source redirects."""
    source_relative = _promotion_safe_relative(source_dir, local_root)
    if source_relative is None:
        return None, "profile-local source is a symlink/junction or escapes its skills root"
    files: List[Tuple[Path, Path]] = []
    try:
        for path in source_dir.rglob("*"):
            relative = path.relative_to(source_dir)
            if _promotion_excluded(relative):
                continue
            if _is_path_redirect(path):
                return None, f"source contains a symlink/junction: {relative}"
            if path.is_dir():
                continue
            if not path.is_file():
                return None, f"source contains unsupported non-file entry: {relative}"
            if relative.name == "SKILL.md" and len(relative.parts) == 1:
                files.append((path, relative))
            elif relative.parts and relative.parts[0] in _PROMOTION_ALLOWED_DIRS:
                files.append((path, relative))
    except OSError as exc:
        return None, f"could not inspect profile-local source: {exc}"
    if not any(relative == Path("SKILL.md") for _, relative in files):
        return None, "profile-local source has no redistributable SKILL.md"
    return sorted(files, key=lambda item: item[1].as_posix()), None


def _promotion_manifest(files: List[Tuple[Path, Path]]) -> Dict[str, Any]:
    entries = []
    for source, relative in files:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        entries.append({"path": relative.as_posix(), "sha256": digest, "bytes": source.stat().st_size})
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"algorithm": "sha256", "files": entries, "package_sha256": hashlib.sha256(canonical).hexdigest()}


def _promotion_target_is_owned(marker: Path, owner_token: str) -> bool:
    """Prove a published target still belongs to this invocation before cleanup."""
    if _is_path_redirect(marker):
        return False
    try:
        return marker.is_file() and marker.read_text(encoding="utf-8") == owner_token
    except OSError:
        return False


def _configured_shared_promotion_root() -> Tuple[Optional[Path], Optional[str]]:
    """Resolve exactly the configured external create_dir, never a caller path."""
    try:
        from hermes_cli.config import load_config
        from agent.skill_utils import get_all_skills_dirs
        configured = cfg_get(load_config(), "skills", "create_dir")
    except Exception as exc:
        return None, f"could not load skills.create_dir: {exc}"
    if not isinstance(configured, str) or not configured.strip():
        return None, "skills.create_dir must be configured to a shared/external skills root before promotion"
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        return None, "skills.create_dir must be an absolute shared/external skills root"
    if _is_path_redirect(candidate):
        return None, "skills.create_dir must not be a symlink/junction"
    try:
        target_root = candidate.resolve(strict=True)
        local_root = _skills_dir().resolve()
        external_roots = {Path(root).resolve() for root in get_all_skills_dirs() if Path(root).exists()}
    except OSError as exc:
        return None, f"could not resolve skills.create_dir safely: {exc}"
    if target_root == local_root or target_root not in external_roots:
        return None, "skills.create_dir must resolve to one configured shared/external skills root, not the profile-local root"
    return target_root, None


def _promote_skill(name: str) -> Dict[str, Any]:
    """Publish one exact profile-local skill to the configured shared root safely."""
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}
    local_root = _skills_dir()
    if _is_path_redirect(local_root):
        return {"success": False, "error": "profile-local skills root is a symlink/junction"}
    try:
        local_root = local_root.resolve(strict=True)
    except OSError as exc:
        return {"success": False, "error": f"could not resolve profile-local skills root: {exc}"}

    # Do not use _find_skill: it may select the shared/external collision.
    sources = []
    try:
        for md in local_root.rglob("SKILL.md"):
            if md.parent.name != name or _promotion_safe_relative(md.parent, local_root) is None:
                continue
            fm = _strict_frontmatter(md.read_text(encoding="utf-8"))
            if fm.get("name") == name:
                sources.append(md.parent)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return {"success": False, "error": f"could not discover exact profile-local source '{name}': {exc}"}
    if len(sources) != 1:
        state = "not found" if not sources else "ambiguous"
        return {"success": False, "error": f"exact profile-local source '{name}' is {state}; promotion requires exactly one source"}
    source_dir = sources[0]
    files, error = _promotion_files(source_dir, local_root)
    if error:
        return {"success": False, "error": error}
    assert files is not None
    target_root, error = _configured_shared_promotion_root()
    if error:
        return {"success": False, "error": error}
    assert target_root is not None
    source_relative = source_dir.relative_to(local_root)
    target_dir = target_root / source_relative
    if _promotion_safe_relative(target_dir.parent, target_root) is None:
        return {"success": False, "error": "resolved destination parent escapes configured shared root"}
    if target_dir.exists() or target_dir.is_symlink() or _is_path_redirect(target_dir):
        return {"success": False, "error": f"refusing to overwrite existing shared target: {target_dir}"}
    # Same-name anywhere in the target root is a collision even across categories.
    try:
        for md in target_root.rglob("SKILL.md"):
            if md.parent.name == name:
                return {"success": False, "error": f"refusing same-name shared collision: {md.parent}"}
    except OSError as exc:
        return {"success": False, "error": f"could not inspect shared target root: {exc}"}

    manifest = _promotion_manifest(files)
    backup_root = get_hermes_home() / "skill-promotion-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_id = f"{name}-{uuid.uuid4().hex}"
    rollback_archive = backup_root / f"{backup_id}.tar.gz"
    rollback_manifest = backup_root / f"{backup_id}.manifest.json"
    try:
        with tarfile.open(rollback_archive, "w:gz") as archive:
            for source, relative in files:
                archive.add(source, arcname=relative.as_posix(), recursive=False)
        atomic_write_text(rollback_manifest, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", create_mode=0o600)
    except (OSError, tarfile.TarError) as exc:
        return {"success": False, "error": f"could not create rollback archive and manifest: {exc}"}

    # Keep temporary output within the configured shared root too: promotion
    # must not create arbitrary sibling directories while it prepares an atomic
    # replacement.
    stage_parent = target_root
    stage_dir = None
    published_target = False
    owner_marker_name = f".{name}.promotion-owner"
    owner_token = uuid.uuid4().hex
    owner_marker = target_dir / owner_marker_name
    try:
        stage_dir = Path(tempfile.mkdtemp(prefix=f".{name}.promote-", dir=stage_parent))
        staged_target = stage_dir / source_relative
        for source, relative in files:
            destination = staged_target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination, follow_symlinks=False)
        # The marker moves atomically with the staged package.  It is excluded
        # from the public manifest and lets failure cleanup distinguish this
        # invocation's output from a target another writer replaced mid-flight.
        staged_marker = staged_target / owner_marker_name
        staged_marker.write_text(owner_token, encoding="utf-8")
        staged_files, stage_error = _promotion_files(staged_target, stage_dir)
        if stage_error or staged_files is None or _promotion_manifest(staged_files) != manifest:
            raise OSError(stage_error or "staging manifest does not match source manifest")
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if target_dir.exists() or _is_path_redirect(target_dir):
            raise FileExistsError(f"shared target appeared during staging: {target_dir}")
        os.replace(staged_target, target_dir)
        published_target = True
        staged_target = None
        published_files, published_error = _promotion_files(target_dir, target_root)
        if published_error or published_files is None or _promotion_manifest(published_files) != manifest:
            raise OSError(published_error or "post-readback manifest mismatch")
        if not _promotion_target_is_owned(owner_marker, owner_token):
            raise OSError("post-readback ownership marker mismatch")
        owner_marker.unlink()
    except (OSError, shutil.Error) as exc:
        if published_target and target_dir.exists() and _promotion_target_is_owned(owner_marker, owner_token):
            shutil.rmtree(target_dir, ignore_errors=True)
        return {"success": False, "error": f"promotion failed; source preserved and owned target rolled back when safe: {exc}",
                "rollback": {"archive": str(rollback_archive), "manifest": str(rollback_manifest)}}
    finally:
        if stage_dir is not None:
            shutil.rmtree(stage_dir, ignore_errors=True)

    return {
        "success": True,
        "message": f"Skill '{name}' promoted to configured shared skills root.",
        "promotion": {
            "source": str(source_dir), "target": str(target_dir), "manifest": manifest,
            "rollback": {"archive": str(rollback_archive), "manifest": str(rollback_manifest)},
            "post_readback": "manifest_sha256_match",
        },
    }


# =============================================================================
# Main entry point
# =============================================================================

# Enforced lifecycle preflight -------------------------------------------------
# This is deliberately core-resident and computes from loader roots/configuration;
# it never consumes an LLM-supplied manifest or user-editable receipt.
_LIFECYCLE_MUTATIONS = {"create", "edit", "patch", "write_file", "delete", "remove_file"}
_LIFECYCLE_WORDS = re.compile(r"[\w-]{3,}", re.UNICODE)


def _strict_frontmatter(content: str) -> Dict[str, Any]:
    """Parse SKILL.md frontmatter without duplicate-key ambiguity."""
    content = content.lstrip("\ufeff")
    if not content.startswith("---"):
        raise ValueError("missing YAML frontmatter")
    end = re.search(r"\n---\s*\n", content[3:])
    if not end:
        raise ValueError("unclosed YAML frontmatter")
    parsed = yaml.load(content[3:end.start() + 3], Loader=_UniqueKeySafeLoader)
    if not isinstance(parsed, dict):
        raise ValueError("frontmatter is not a mapping")
    return parsed


def _lifecycle_text(name: str, description: str, body: str) -> set[str]:
    # Structured sections are intentionally part of the deterministic signal,
    # rather than an LLM's free-form assertion that two skills differ.
    headings = re.findall(r"^##?\s+(.+)$", body, re.MULTILINE)
    return set(x.lower() for x in _LIFECYCLE_WORDS.findall(" ".join([name, description, *headings, body])))


def _lifecycle_content_fingerprint(content: str) -> str:
    """Detect exact procedural copies under different names, not topic overlap."""
    fm = _strict_frontmatter(content)
    end = re.search(r"\n---\s*\n", content.lstrip("\ufeff")[3:])
    assert end is not None  # _strict_frontmatter validated the closing delimiter
    body = content.lstrip("\ufeff")[end.end() + 3:].strip()
    identity = json.dumps([str(fm.get("description", "")).strip(), body], ensure_ascii=False)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _core_skill_inventory() -> List[Dict[str, Any]]:
    """Inventory every applicable loader root, including disabled and archive.

    External/project roots are discovery sources, not write authority.  A
    malformed or escaping SKILL.md is recorded as invalid so the preflight
    blocks rather than silently hiding it.
    """
    from agent.skill_utils import get_all_skills_dirs, get_disabled_skill_names
    roots = []
    for root in get_all_skills_dirs():
        try:
            resolved = Path(root).resolve()
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    local = _skills_dir().resolve()
    disabled = set(get_disabled_skill_names())
    records = []
    for root in roots:
        if not root.is_dir():
            continue
        source = "local" if root == local else "external"
        for md in root.rglob("SKILL.md"):
            try:
                resolved = md.resolve()
                if resolved != root and root not in resolved.parents:
                    records.append({"path": str(md), "state": "invalid", "reason": "symlink escape"})
                    continue
                text = resolved.read_text(encoding="utf-8")
                fm = _strict_frontmatter(text)
                skill_name = str(fm.get("name") or resolved.parent.name)
                archived = ".archive" in resolved.relative_to(root).parts
                readonly = source != "local"
                try:
                    from tools import skill_usage
                    readonly = readonly or skill_usage.is_bundled(skill_name) or skill_usage.is_hub_installed(skill_name)
                except Exception:
                    pass
                records.append({"name": skill_name, "description": str(fm.get("description", "")),
                                "path": str(resolved), "source": source, "disabled": skill_name in disabled,
                                "archived": archived, "readonly": readonly,
                                "provenance": fm.get("provenance") or fm.get("homepage") or fm.get("author"),
                                "content_fingerprint": _lifecycle_content_fingerprint(text),
                                "terms": sorted(_lifecycle_text(skill_name, str(fm.get("description", "")), text))})
            except (OSError, ValueError, yaml.YAMLError) as exc:
                records.append({"path": str(md), "state": "invalid", "reason": str(exc)})
    return sorted(records, key=lambda r: (r.get("name", ""), r["path"]))


def _core_lifecycle_preflight(action: str, name: str, content: Optional[str], file_path: Optional[str]) -> Dict[str, Any]:
    """Return a deterministic decision used in the same mutation call.

    The result is evidence, not an authorization supplied by the caller.
    Unsupported state transitions are returned as fail-closed recommendations.
    """
    if action not in _LIFECYCLE_MUTATIONS:
        return {"allowed": True, "decision": action, "candidates": []}
    if action in {"delete", "remove_file"}:
        # Destructive user-directed operations have an exact target; unrelated
        # malformed skills must not block a legitimate profile-local cleanup.
        # The low-level helpers repeat this guard before filesystem mutation.
        existing = _find_skill(name)
        if existing:
            guard = _external_destructive_write_guard(name, existing["path"], action)
            if guard:
                return {"allowed": False, "decision": "blocked", "source_state": "read_only",
                        "reason": guard["error"], "candidates": []}
        return {"allowed": True, "decision": action, "candidates": []}
    try:
        proposed = _strict_frontmatter(content) if action in {"create", "edit"} and content else {}
    except (ValueError, yaml.YAMLError) as exc:
        return {"allowed": False, "reason": f"invalid proposed frontmatter: {exc}", "decision": "blocked", "candidates": []}
    description = str(proposed.get("description", ""))
    terms = _lifecycle_text(name, description, content or "")
    inventory = _core_skill_inventory()
    invalid = [r for r in inventory if r.get("state") == "invalid"]
    if invalid:
        return {"allowed": False, "reason": "inventory contains invalid/escaping skill metadata", "decision": "blocked", "candidates": invalid, "source_state": "invalid"}
    candidates = []
    for r in inventory:
        score = 1.0 if r["name"] == name else len(terms & set(r["terms"])) / max(1, len(terms))
        if r["name"] == name or score >= 0.30:
            candidates.append(dict(r, similarity=round(score, 4)))
    exact = [r for r in candidates if r["name"] == name]
    # Topic words (especially those in a long procedure body) are discovery
    # hints, not evidence that a distinct skill is the same procedure. Only an
    # exact name or an identical description+body can veto a create.
    duplicate = []
    if action == "create" and content and not exact:
        fingerprint = _lifecycle_content_fingerprint(content)
        duplicate = [r for r in inventory if r["content_fingerprint"] == fingerprint]
    pool = exact or duplicate
    # A profile-local skill shadows the same-name external copy in the loader;
    # lexical path order must not make an unrelated shared root the target.
    pool.sort(key=lambda r: (r["source"] != "local", r["name"], r["path"]))
    local_count = sum(r["source"] == "local" for r in pool)
    if len(pool) > 1 and (len({r["name"] for r in pool}) > 1 or local_count > 1 or local_count == 0):
        return {"allowed": False, "decision": "blocked", "reason": "ambiguous matching skills; choose a target explicitly", "candidates": pool, "source_state": "ambiguous"}
    selected = pool[0] if pool else None
    evidence = {"decision": action, "candidates": candidates, "selected": selected,
                "evidence_locator": "core:skill_manager_tool:_core_lifecycle_preflight", "source_state": "none"}
    if not selected:
        return dict(evidence, allowed=True, reason="no exact name or identical procedure; similar topics are advisory")
    if selected["disabled"]:
        return dict(evidence, allowed=False, decision="enable", source_state="disabled", reason="matching skill is disabled", recommended_action="enable", recommended_target=selected["path"])
    if selected["archived"]:
        return dict(evidence, allowed=False, decision="restore", source_state="archived", reason="matching skill is archived", recommended_action="restore", recommended_target=selected["path"])
    if selected["readonly"]:
        return dict(evidence, allowed=False, decision="fork", source_state="read_only", reason="matching skill is read-only", recommended_action="fork", recommended_target=selected["path"])
    if action == "create":
        return dict(evidence, allowed=False, decision="patch", source_state="editable", reason="matching editable skill exists", recommended_action="patch", recommended_target=selected["path"])
    return dict(evidence, allowed=True, source_state="editable", reason="selected editable skill", recommended_target=selected["path"])


# ContextVar bypass: set while replaying an already-approved staged skill write
# so skill_manage() does not re-gate (and re-stage) it.
import contextvars as _ctxvars
_skill_gate_bypass: "_ctxvars.ContextVar[bool]" = _ctxvars.ContextVar(
    "skill_gate_bypass", default=False
)


def _apply_skill_write_gate(action, name, **payload_kwargs):
    """Evaluate the skill write gate. Returns a JSON tool-result string when the
    write should NOT proceed (blocked or staged), or None to perform the real
    write. Bypassed during approved-pending replay.
    """
    if action not in {"create", "edit", "patch", "delete", "write_file", "remove_file", "promote", "update_shared"}:
        return None
    if _skill_gate_bypass.get():
        return None

    try:
        from tools import write_approval as wa
    except Exception:
        return None  # fail open

    decision = wa.evaluate_gate(wa.SKILLS)
    if decision.allow:
        return None
    if decision.blocked:
        return tool_error(decision.message, success=False)

    # stage — record the full skill_manage kwargs so approval can replay it.
    payload = {"action": action, "name": name}
    payload.update({k: v for k, v in payload_kwargs.items() if v is not None})
    gist = wa.skill_gist(
        action, name,
        content=payload_kwargs.get("content") or "",
        file_path=payload_kwargs.get("file_path") or "",
        old_string=payload_kwargs.get("old_string") or "",
        new_string=payload_kwargs.get("new_string") or "",
    )
    record = wa.stage_write(wa.SKILLS, payload, summary=gist, origin=wa.current_origin())
    return json.dumps(
        {"success": True, "staged": True, "pending_id": record["id"],
         "gist": gist, "message": decision.message},
        ensure_ascii=False,
    )


def apply_skill_pending(payload: Dict[str, Any]) -> str:
    """Replay a staged skill write, bypassing the gate. Returns the tool result
    JSON string. Called by the /skills approve handler.
    """
    token = _skill_gate_bypass.set(True)
    try:
        return skill_manage(
            action=payload.get("action", ""),
            name=payload.get("name", ""),
            content=payload.get("content"),
            category=payload.get("category"),
            file_path=payload.get("file_path"),
            file_content=payload.get("file_content"),
            old_string=payload.get("old_string"),
            new_string=payload.get("new_string"),
            replace_all=payload.get("replace_all", False),
            absorbed_into=payload.get("absorbed_into"),
            shared_path=payload.get("shared_path"),
            expected_sha256=payload.get("expected_sha256"),
            rollback_id=payload.get("rollback_id"),
        )
    finally:
        _skill_gate_bypass.reset(token)


# Debounce state for the sync push hook. A burst of skill_manage writes
# (e.g. create + several write_file calls) collapses into a single push after
# a short quiet window, on a daemon timer so the agent write never blocks.
_sync_push_timer = None
_sync_push_lock = None
_SYNC_PUSH_DEBOUNCE_S = 5.0


def _maybe_debounced_sync_push(skill_name: str) -> None:
    """Schedule a debounced best-effort sync push after a skill write.

    Cheap fast-path: if the skill isn't opted into sync, do nothing (no auth,
    no network). Otherwise (re)arm a daemon timer; the actual push runs through
    ``skills_sync_client.maybe_push_skills`` which enforces the access gate
    and swallows all errors. Never blocks the caller (M1-C: agent never blocks
    on sync).
    """
    global _sync_push_timer, _sync_push_lock
    try:
        from tools.skill_usage import is_sync_enabled

        if not is_sync_enabled(skill_name):
            return
    except Exception:
        return

    import threading

    if _sync_push_lock is None:
        _sync_push_lock = threading.Lock()

    def _fire():
        try:
            from tools.skills_sync_client import maybe_push_skills

            maybe_push_skills(message=f"sync: {skill_name}")
        except Exception:
            pass

    with _sync_push_lock:
        if _sync_push_timer is not None:
            try:
                _sync_push_timer.cancel()
            except Exception:
                pass
        _sync_push_timer = threading.Timer(_SYNC_PUSH_DEBOUNCE_S, _fire)
        _sync_push_timer.daemon = True
        _sync_push_timer.start()


def skill_manage(
    action: str,
    name: str,
    content: str = None,
    category: str = None,
    file_path: str = None,
    file_content: str = None,
    old_string: str = None,
    new_string: str = None,
    replace_all: bool = False,
    absorbed_into: str = None,
    task_id: str = None,
    session_id: str = None,
    shared_path: str = None,
    expected_sha256: str = None,
    rollback_id: str = None,
) -> str:
    """
    Manage user-created skills. Dispatches to the appropriate action handler.

    Returns JSON string with results.
    """
    if action == "preflight":
        # Read-only explicit inspection surface. Mutations below independently
        # recompute this same evidence in their own call, so this result cannot
        # be forged/replayed as authority.
        lifecycle = _core_lifecycle_preflight("create" if content else "patch", name, content, file_path)
        return json.dumps({"success": True, "preflight": lifecycle}, ensure_ascii=False)

    if action == "update_shared":
        # A distinct, narrow maintenance lane: normal edit/patch remains
        # read-only for external skills. Only configured shared create_dir can
        # be targeted; arbitrary profiles' create_dir points at their own
        # local root and is rejected by this resolver.
        from tools.skill_provenance import is_background_review
        if get_hermes_home().name != "10-skill-librarian":
            return tool_error("update_shared is restricted to the skill-librarian profile", success=False)
        if is_background_review():
            return tool_error("background review cannot update shared skills", success=False)
        root, error = _configured_shared_promotion_root()
        if error:
            return tool_error(error, success=False)
        gate_result = _apply_skill_write_gate(
            action, name, content=content, shared_path=shared_path,
            expected_sha256=expected_sha256, rollback_id=rollback_id,
        )
        if gate_result is not None:
            return gate_result
        from tools.managed_shared_skill_update import managed_shared_update
        result = managed_shared_update(root, name, shared_path, expected_sha256,
                                      content=content, rollback_id=rollback_id)
        if result.get("success"):
            try:
                from agent.prompt_builder import clear_skills_system_prompt_cache
                clear_skills_system_prompt_cache(clear_snapshot=True)
            except Exception:
                pass
        return json.dumps(result, ensure_ascii=False)

    lifecycle = _core_lifecycle_preflight(action, name, content, file_path)
    if not lifecycle.get("allowed"):
        return json.dumps({"success": False, "error": lifecycle.get("reason"), "preflight": lifecycle}, ensure_ascii=False)

    preflight = _background_review_preflight(action, name)
    if preflight is not None:
        return json.dumps(preflight, ensure_ascii=False)

    # Approval gate: when on, stages the write for review (skills are too large
    # to review inline, so they always stage regardless of origin); when off
    # (default) passes straight through. The gate is bypassed when this call is
    # itself replaying an already-approved staged write (_skill_apply_pending).
    gate_result = _apply_skill_write_gate(
        action, name, content=content, category=category,
        file_path=file_path, file_content=file_content,
        old_string=old_string, new_string=new_string,
        replace_all=replace_all, absorbed_into=absorbed_into,
    )
    if gate_result is not None:
        return gate_result

    # Audit ledger (tracker #79686 P3): capture the pre-mutation state of the
    # skill directory so every mutation — any actor — lands in the append-only
    # JSONL ledger with before/after blobs. Telemetry, not a gate: failures
    # here must NEVER block the mutation (capture_before returns None on
    # error, and record_mutation below swallows everything).
    _ledger_before = None
    _ledger_before_dir = None
    try:
        from tools import skill_ledger as _ledger
        _pre = _find_skill(name)
        _ledger_before_dir = _pre["path"] if _pre else None
        _ledger_before = _ledger.capture_before(_ledger_before_dir)
    except Exception:
        pass

    if action == "create":
        if not content:
            return tool_error("content is required for 'create'. Provide the full SKILL.md text (frontmatter + body).", success=False)
        result = _create_skill(name, content, category)

    elif action == "edit":
        if not content:
            return tool_error("content is required for 'edit'. Provide the full updated SKILL.md text.", success=False)
        result = _edit_skill(name, content)

    elif action == "patch":
        if not old_string:
            return tool_error("old_string is required for 'patch'. Provide the text to find.", success=False)
        if new_string is None:
            return tool_error("new_string is required for 'patch'. Use empty string to delete matched text.", success=False)
        result = _patch_skill(name, old_string, new_string, file_path, replace_all)

    elif action == "delete":
        result = _delete_skill(name, absorbed_into=absorbed_into)

    elif action == "write_file":
        if not file_path:
            return tool_error("file_path is required for 'write_file'. Example: 'references/api-guide.md'", success=False)
        if file_content is None:
            return tool_error("file_content is required for 'write_file'.", success=False)
        result = _write_file(name, file_path, file_content)

    elif action == "remove_file":
        if not file_path:
            return tool_error("file_path is required for 'remove_file'.", success=False)
        result = _remove_file(name, file_path)

    elif action == "promote":
        result = _promote_skill(name)

    else:
        result = {"success": False, "error": f"Unknown action '{action}'. Use: preflight, create, patch, edit, delete, write_file, remove_file, promote"}

    if result.get("success"):
        # Persisted result is bound to evidence computed by this core call, not
        # to a mutable profile receipt.
        result["preflight"] = lifecycle
        # Audit ledger append (best-effort; never blocks the mutation).
        try:
            from tools import skill_ledger as _ledger
            _post = _find_skill(name)
            _after_dir = _post["path"] if _post else None
            _evidence = {}
            if action == "delete":
                # Record delete intent: consolidation vs prune, and whether
                # the recoverable-archive path handled it (curator pass).
                _evidence["absorbed_into"] = absorbed_into
                _evidence["archived"] = bool(result.get("_archived"))
            if session_id:
                _evidence["session_id"] = session_id
            if file_path:
                _evidence["file_path"] = file_path
            _ledger.record_mutation(
                action,
                name,
                before=_ledger_before if _ledger_before is not None else [],
                after_root=_after_dir,
                evidence=_evidence,
            )
        except Exception:
            pass
        try:
            from agent.prompt_builder import clear_skills_system_prompt_cache
            clear_skills_system_prompt_cache(clear_snapshot=True)
        except Exception:
            pass
        # Curator telemetry: bump patch_count on edit/patch/write_file (the actions
        # that mutate an existing skill's guidance), drop the record on delete.
        # Only mark a skill as agent-created when the background self-improvement
        # review fork creates it — foreground `skill_manage(create)` calls are
        # user-directed, and those skills belong to the user (the curator must
        # not touch them). Best-effort; telemetry failures never break the tool.
        try:
            from tools.skill_usage import bump_patch, forget, record_created
            from tools.skill_provenance import is_background_review
            if action == "create":
                record_created(
                    name,
                    agent_created=is_background_review(),
                    task_id=task_id,
                    session_id=session_id,
                )
            elif action in {"patch", "edit", "write_file", "remove_file"}:
                bump_patch(
                    name,
                    action=action,
                    task_id=task_id,
                    session_id=session_id,
                )
            elif action == "delete":
                # A recoverable curator archive (routed through archive_skill)
                # keeps its usage record as STATE_ARCHIVED so `hermes curator
                # status`/`restore` still see it. Only a hard delete forgets.
                if not result.get("_archived"):
                    forget(name)
        except Exception:
            pass

        # Sync push hook (debounced, best-effort). Fires only AFTER the
        # write gate passed (staged/unapproved writes never reach here -- the
        # gate returns early above), so we never push un-reviewed content.
        # Inert unless the access gate is open (the user is a Nous admin on the
        # token), a sync base URL is configured, and the skill is opted into
        # sync. Debounced so a burst of edits collapses to one push. Never
        # raises -- an agent write must never block on sync (M1-C invariant).
        try:
            _maybe_debounced_sync_push(name)
        except Exception:
            pass

    return json.dumps(result, ensure_ascii=False)


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

SKILL_MANAGE_SCHEMA = {
    "name": "skill_manage",
    "description": (
        "Manage skills (create, update, delete). Skills are your procedural "
        "memory — reusable approaches for recurring task types. "
        f"New skills go to {display_hermes_home()}/skills/; existing skills can be modified wherever they live.\n\n"
        "Actions: preflight (inspect enforced lifecycle decision), create (full SKILL.md + optional category), "
        "patch (old_string/new_string — preferred for fixes), "
        "edit (full SKILL.md rewrite — major overhauls only), "
        "delete, write_file, remove_file, promote, update_shared. Update_shared "
        "replaces one existing configured shared SKILL.md with CAS and durable "
        "backup/journal; rollback needs resulting hash and transaction id. "
        "Promote publishes exactly one "
        "profile-local skill to configured external skills.create_dir with a rollback archive; it never accepts a destination path.\n\n"
        "On delete, pass `absorbed_into=<umbrella>` when you're merging this "
        "skill's content into another one, or `absorbed_into=\"\"` when you're "
        "pruning it with no forwarding target. This lets the curator tell "
        "consolidation from pruning without guessing, so downstream consumers "
        "(cron jobs that reference the old skill name, etc.) get updated "
        "correctly. The target you name in `absorbed_into` must already "
        "exist — create/patch the umbrella first, then delete.\n\n"
        "Create when: complex task succeeded (5+ calls), errors overcome, "
        "user-corrected approach worked, non-trivial workflow discovered, "
        "or user asks you to remember a procedure.\n"
        "Update when: instructions stale/wrong, OS-specific failures, "
        "missing steps or pitfalls found during use. "
        "If you used a skill and hit issues not covered by it, patch it immediately.\n\n"
        "After difficult/iterative tasks, offer to save as a skill. "
        "Skip for simple one-offs. Confirm with user before creating/deleting.\n\n"
        "Good skills: trigger conditions, numbered steps with exact commands, "
        "pitfalls section, verification steps. Use skill_view() to see format examples.\n\n"
        "Description: long descriptions are truncated to the first 57 chars "
        "plus '...' in the system prompt skill index; longer text is visible "
        "via skills_list/skill_view. Keep the trigger self-contained in that "
        "first 57-char window: 'Use when <trigger>. <one-line behavior>.'\n\n"
        "Pinned skills are protected from deletion only — skill_manage(action='delete') "
        "will refuse with a message pointing the user to `hermes curator unpin <name>`. "
        "Patches and edits go through on pinned skills so you can still improve them as "
        "pitfalls come up; pin only guards against irrecoverable loss."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["preflight", "create", "patch", "edit", "delete", "write_file", "remove_file", "promote", "update_shared"],
                "description": "The action to perform."
            },
            "name": {
                "type": "string",
                "description": (
                    "Skill name (lowercase, hyphens/underscores, max 64 chars). "
                    "Must match an existing skill for patch/edit/delete/write_file/remove_file."
                )
            },
            "content": {
                "type": "string",
                "description": (
                    "Full SKILL.md content (YAML frontmatter + markdown body). "
                    "Required for 'create' and 'edit'. For 'edit', read the skill "
                    "first with skill_view() and provide the complete updated text."
                )
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Text to find in the file (required for 'patch'). Must be unique "
                    "unless replace_all=true. Include enough surrounding context to "
                    "ensure uniqueness."
                )
            },
            "new_string": {
                "type": "string",
                "description": (
                    "Replacement text (required for 'patch'); must differ from "
                    "old_string. Can be empty string to delete the matched text."
                )
            },
            "replace_all": {
                "type": "boolean",
                "description": "For 'patch': replace all occurrences instead of requiring a unique match (default: false)."
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional category/domain for organizing the skill (e.g., 'devops', "
                    "'data-science', 'mlops'). Creates a subdirectory grouping. "
                    "Only used with 'create'."
                )
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Path to a supporting file within the skill directory. "
                    "For 'write_file'/'remove_file': required, must be under references/, "
                    "templates/, scripts/, or assets/. "
                    "For 'patch': optional, defaults to SKILL.md if omitted."
                )
            },
            "file_content": {
                "type": "string",
                "description": "Content for the file. Required for 'write_file'."
            },
            "absorbed_into": {
                "type": "string",
                "description": (
                    "For 'delete' only — declares intent so the curator can "
                    "tell consolidation from pruning without guessing. "
                    "Pass the umbrella skill name when this skill's content "
                    "was merged into another (the target must already exist). "
                    "Pass an empty string when the skill is truly stale and "
                    "being pruned with no forwarding target. Omitting the arg "
                    "on delete is supported for backward compatibility but "
                    "downstream tooling (e.g. cron-job skill reference "
                    "rewriting) will have to guess at intent."
                )
            },
            "shared_path": {
                "type": "string",
                "description": "For update_shared: existing category/name/SKILL.md under configured external skills.create_dir."
            },
            "expected_sha256": {
                "type": "string",
                "description": "For update_shared: lowercase SHA-256 of current shared SKILL.md; required also for rollback."
            },
            "rollback_id": {
                "type": "string",
                "description": "For update_shared rollback only: returned transaction id; omit content."
            },
        },
        "required": ["action", "name"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="skill_manage",
    toolset="skills",
    schema=SKILL_MANAGE_SCHEMA,
    handler=lambda args, **kw: skill_manage(
        action=args.get("action", ""),
        name=args.get("name", ""),
        content=args.get("content"),
        category=args.get("category"),
        file_path=args.get("file_path"),
        file_content=args.get("file_content"),
        old_string=args.get("old_string"),
        new_string=args.get("new_string"),
        replace_all=args.get("replace_all", False),
        absorbed_into=args.get("absorbed_into"),
        shared_path=args.get("shared_path"),
        expected_sha256=args.get("expected_sha256"),
        rollback_id=args.get("rollback_id"),
        task_id=kw.get("task_id"),
        session_id=kw.get("session_id")),
    emoji="📝",
)
