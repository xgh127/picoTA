"""Versioned, integrity-checked Skill registry for Context compilation.

Skill metadata is trusted configuration, not a path supplied by a request.
Entrypoints are resolved relative to the package Skill directory or an
explicitly registered root and must point to a ``SKILL.md`` file inside that
root.  URLs, absolute paths, traversal and symlink escapes are rejected.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .errors import ContextError

SKILL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]*$")
CONTEXT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
ROOT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
CHECKSUM_PATTERN = re.compile(r"^(?:sha256:)?(?P<digest>[0-9a-fA-F]{64})$")

SKILL_METADATA_FIELDS = frozenset(
    {
        "skill_id",
        "version",
        "entrypoint",
        "checksum",
        "enabled",
        "required_context",
        "allowed_tools",
    }
)


class SkillRegistryError(ContextError):
    """Base class for Skill registry failures."""

    code = "skill_registry_error"


class SkillMetadataValidationError(ValueError, SkillRegistryError):
    """Skill metadata is malformed or unsafe."""

    code = "skill_metadata_invalid"


class SkillPathError(ValueError, SkillRegistryError):
    """A Skill entrypoint is outside its registered root."""

    code = "skill_path_invalid"


class SkillNotFoundError(KeyError, SkillRegistryError):
    """No registered Skill matches the requested ID and version."""

    code = "skill_not_found"


class SkillDisabledError(PermissionError, SkillRegistryError):
    """The requested Skill version exists but is disabled."""

    code = "skill_disabled"


class SkillIntegrityError(ValueError, SkillRegistryError):
    """The on-disk Skill content does not match its checksum."""

    code = "skill_integrity_error"


class SkillConstraintError(ValueError, SkillRegistryError):
    """Skill metadata attempts to expand a Recipe's authority."""

    code = "skill_constraint_error"


@dataclass(frozen=True)
class SkillMetadata:
    skill_id: str
    version: int
    entrypoint: str
    checksum: str
    enabled: bool
    required_context: tuple[str, ...]
    allowed_tools: tuple[str, ...]


class LoadedSkill(NamedTuple):
    skill_content: str
    metadata: SkillMetadata
    checksum: str


@dataclass(frozen=True)
class _RegistryEntry:
    metadata: SkillMetadata
    root_name: str


def compute_skill_checksum(content: str | bytes) -> str:
    payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normalise_checksum(value: object) -> str:
    if not isinstance(value, str):
        raise SkillMetadataValidationError("checksum must be a sha256 string")
    match = CHECKSUM_PATTERN.fullmatch(value)
    if not match:
        raise SkillMetadataValidationError("checksum must contain exactly one SHA-256 digest")
    return "sha256:" + match.group("digest").lower()


def _validate_entrypoint(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SkillMetadataValidationError("entrypoint must be a non-empty relative path")
    if "\\" in value or ":" in value:
        raise SkillPathError("entrypoint must not be a URL or platform-specific path")
    raw_parts = value.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise SkillPathError("entrypoint must not contain empty, '.' or '..' path segments")
    entrypoint = PurePosixPath(value)
    if entrypoint.is_absolute() or entrypoint.name != "SKILL.md":
        raise SkillPathError("entrypoint must be a relative path ending in SKILL.md")
    return entrypoint.as_posix()


def _metadata_names(value: object, field_name: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and pattern.fullmatch(item) for item in value
    ):
        raise SkillMetadataValidationError(f"{field_name} must be a list of valid names")
    if len(value) != len(set(value)):
        raise SkillMetadataValidationError(f"{field_name} must not contain duplicates")
    return tuple(value)


def validate_skill_metadata(data: Mapping[str, object] | SkillMetadata) -> SkillMetadata:
    """Validate and normalize a complete Skill metadata record."""
    if isinstance(data, SkillMetadata):
        data = {
            "skill_id": data.skill_id,
            "version": data.version,
            "entrypoint": data.entrypoint,
            "checksum": data.checksum,
            "enabled": data.enabled,
            "required_context": list(data.required_context),
            "allowed_tools": list(data.allowed_tools),
        }
    if not isinstance(data, Mapping):
        raise SkillMetadataValidationError("skill metadata must be a mapping")

    keys = set(data)
    missing = SKILL_METADATA_FIELDS - keys
    if missing:
        raise SkillMetadataValidationError(f"skill metadata is missing fields: {sorted(missing)!r}")
    unknown = keys - SKILL_METADATA_FIELDS
    if unknown:
        raise SkillMetadataValidationError(f"skill metadata has unknown fields: {sorted(unknown)!r}")

    skill_id = data["skill_id"]
    if not isinstance(skill_id, str) or not SKILL_ID_PATTERN.fullmatch(skill_id):
        raise SkillMetadataValidationError("skill_id must be a stable lowercase identifier")
    version = data["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise SkillMetadataValidationError("version must be a positive integer")
    enabled = data["enabled"]
    if not isinstance(enabled, bool):
        raise SkillMetadataValidationError("enabled must be a boolean")

    return SkillMetadata(
        skill_id=skill_id,
        version=version,
        entrypoint=_validate_entrypoint(data["entrypoint"]),
        checksum=_normalise_checksum(data["checksum"]),
        enabled=enabled,
        required_context=_metadata_names(
            data["required_context"], "required_context", CONTEXT_NAME_PATTERN
        ),
        allowed_tools=_metadata_names(data["allowed_tools"], "allowed_tools", TOOL_NAME_PATTERN),
    )


class SkillRegistry:
    """Registry of immutable Skill metadata keyed by ``(skill_id, version)``."""

    def __init__(self, package_root: str | Path | None = None, *, roots: Mapping[str, str | Path] | None = None):
        default_root = Path(package_root) if package_root is not None else Path(__file__).with_name("skills")
        self._roots: dict[str, Path] = {"package": default_root}
        self._entries: dict[tuple[str, int], _RegistryEntry] = {}
        for name, root in (roots or {}).items():
            self.register_root(name, root)

    def register_root(self, name: str, root: str | Path) -> None:
        if not isinstance(name, str) or not ROOT_NAME_PATTERN.fullmatch(name):
            raise SkillPathError("registered root name must be a stable lowercase identifier")
        if name == "package" or name in self._roots:
            raise SkillPathError(f"skill root is already registered: {name!r}")
        root_path = Path(root)
        if not root_path.is_dir():
            raise SkillPathError(f"registered skill root is not a directory: {root_path}")
        self._roots[name] = root_path

    def register(self, metadata: Mapping[str, object] | SkillMetadata, *, root: str = "package") -> SkillMetadata:
        validated = validate_skill_metadata(metadata)
        if root not in self._roots:
            raise SkillPathError(f"skill root is not registered: {root!r}")
        key = (validated.skill_id, validated.version)
        if key in self._entries:
            raise SkillMetadataValidationError(
                f"skill version is already registered: {validated.skill_id}@{validated.version}"
            )
        self._entries[key] = _RegistryEntry(metadata=validated, root_name=root)
        return validated

    def get_metadata(self, skill_id: str, version: int) -> SkillMetadata:
        _validate_skill_key(skill_id, version)
        entry = self._entries.get((skill_id, version))
        if entry is None:
            raise SkillNotFoundError(f"skill is not registered: {skill_id}@{version}")
        return entry.metadata

    def load(self, skill_id: str, version: int) -> LoadedSkill:
        _validate_skill_key(skill_id, version)
        entry = self._entries.get((skill_id, version))
        if entry is None:
            raise SkillNotFoundError(f"skill is not registered: {skill_id}@{version}")
        metadata = entry.metadata
        if not metadata.enabled:
            raise SkillDisabledError(f"skill is disabled: {skill_id}@{version}")

        root = self._roots[entry.root_name].resolve()
        candidate = root.joinpath(*PurePosixPath(metadata.entrypoint).parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SkillPathError(
                f"skill entrypoint is missing or outside registered root: {metadata.entrypoint!r}"
            ) from exc
        if not resolved.is_file():
            raise SkillPathError(f"skill entrypoint is not a file: {metadata.entrypoint!r}")

        try:
            payload = resolved.read_bytes()
            content = payload.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SkillPathError(f"skill entrypoint is not readable UTF-8: {metadata.entrypoint!r}") from exc

        actual_checksum = compute_skill_checksum(payload)
        if not hmac.compare_digest(actual_checksum, metadata.checksum):
            raise SkillIntegrityError(
                f"skill checksum mismatch for {metadata.skill_id}@{metadata.version}"
            )
        return LoadedSkill(content, metadata, actual_checksum)


def _validate_skill_key(skill_id: object, version: object) -> None:
    if not isinstance(skill_id, str) or not SKILL_ID_PATTERN.fullmatch(skill_id):
        raise SkillMetadataValidationError("skill_id must be a stable lowercase identifier")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise SkillMetadataValidationError("version must be a positive integer")


def apply_skill_constraints(recipe, metadata: SkillMetadata):
    """Apply a Skill without allowing it to expand a Recipe.

    Required Context may only be promoted from the Recipe's optional set,
    and the Skill's tools must be a subset of the Recipe's allowlist.
    """
    metadata = validate_skill_metadata(metadata)
    if not metadata.enabled:
        raise SkillDisabledError(f"skill is disabled: {metadata.skill_id}@{metadata.version}")

    declared_context = set(recipe.required) | set(recipe.optional)
    undeclared = set(metadata.required_context) - declared_context
    if undeclared:
        raise SkillConstraintError(
            f"skill requires Context not authorized by recipe: {sorted(undeclared)!r}"
        )
    extra_tools = set(metadata.allowed_tools) - set(recipe.allowed_tools)
    if extra_tools:
        raise SkillConstraintError(f"skill allows tools not authorized by recipe: {sorted(extra_tools)!r}")

    promoted = tuple(item for item in metadata.required_context if item not in recipe.required)
    return replace(
        recipe,
        required=tuple(recipe.required) + promoted,
        optional=tuple(item for item in recipe.optional if item not in promoted),
        allowed_tools=tuple(tool for tool in recipe.allowed_tools if tool in metadata.allowed_tools),
    )


DEFAULT_SKILL_REGISTRY = SkillRegistry()


def load_skill(skill_id: str, version: int, *, registry: SkillRegistry | None = None) -> LoadedSkill:
    """Load an enabled, registered Skill and verify its SHA-256 checksum."""
    return (registry or DEFAULT_SKILL_REGISTRY).load(skill_id, version)
