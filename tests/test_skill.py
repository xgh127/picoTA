from pathlib import Path

import pytest

from pico.context.recipe import default_recipe_loader
from pico.context.skill import (
    SkillConstraintError,
    SkillDisabledError,
    SkillIntegrityError,
    SkillMetadataValidationError,
    SkillPathError,
    SkillRegistry,
    apply_skill_constraints,
    compute_skill_checksum,
    load_skill,
    validate_skill_metadata,
)


def skill_metadata(content, **updates):
    metadata = {
        "skill_id": "daily-report",
        "version": 2,
        "entrypoint": "daily-report/SKILL.md",
        "checksum": compute_skill_checksum(content),
        "enabled": True,
        "required_context": ["memory_card"],
        "allowed_tools": ["read_file"],
    }
    metadata.update(updates)
    return metadata


def write_skill(root: Path, content="# Daily report\n") -> str:
    path = root / "daily-report" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    return content


def test_registered_skill_loads_content_metadata_and_verified_checksum(tmp_path):
    content = write_skill(tmp_path)
    registry = SkillRegistry(tmp_path)
    metadata = registry.register(skill_metadata(content))

    loaded = load_skill("daily-report", 2, registry=registry)

    assert loaded.skill_content == content
    assert loaded.metadata == metadata
    assert loaded.checksum == compute_skill_checksum(content)
    assert tuple(loaded) == (content, metadata, compute_skill_checksum(content))


def test_skill_can_load_from_an_explicitly_registered_root(tmp_path):
    package_root = tmp_path / "package"
    package_root.mkdir()
    custom_root = tmp_path / "custom"
    custom_root.mkdir()
    content = write_skill(custom_root)
    registry = SkillRegistry(package_root, roots={"custom": custom_root})
    registry.register(skill_metadata(content), root="custom")

    assert registry.load("daily-report", 2).skill_content == content


@pytest.mark.parametrize(
    "entrypoint",
    [
        "https://example.test/SKILL.md",
        "file:///tmp/SKILL.md",
        "/tmp/SKILL.md",
        "../SKILL.md",
        "skill/../SKILL.md",
        "skill/instructions.md",
        "skill\\SKILL.md",
    ],
)
def test_skill_metadata_rejects_unsafe_or_noncanonical_entrypoints(entrypoint):
    with pytest.raises((SkillMetadataValidationError, SkillPathError)):
        validate_skill_metadata(skill_metadata("content", entrypoint=entrypoint))


def test_skill_registry_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("outside", encoding="utf-8")
    root = tmp_path / "root"
    (root / "daily-report").mkdir(parents=True)
    (root / "daily-report" / "SKILL.md").symlink_to(outside / "SKILL.md")
    registry = SkillRegistry(root)
    registry.register(skill_metadata("outside"))

    with pytest.raises(SkillPathError):
        registry.load("daily-report", 2)


def test_skill_checksum_mismatch_fails_closed(tmp_path):
    content = write_skill(tmp_path)
    registry = SkillRegistry(tmp_path)
    registry.register(skill_metadata(content, checksum=compute_skill_checksum("different")))

    with pytest.raises(SkillIntegrityError):
        registry.load("daily-report", 2)


def test_disabled_skill_cannot_be_loaded(tmp_path):
    content = write_skill(tmp_path)
    registry = SkillRegistry(tmp_path)
    registry.register(skill_metadata(content, enabled=False))

    with pytest.raises(SkillDisabledError):
        registry.load("daily-report", 2)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 0),
        ("version", True),
        ("enabled", "yes"),
        ("checksum", "sha256:not-a-digest"),
        ("required_context", "session_checkpoint"),
        ("allowed_tools", ["Read File"]),
    ],
)
def test_skill_metadata_fields_are_strictly_validated(field, value):
    with pytest.raises(SkillMetadataValidationError):
        validate_skill_metadata(skill_metadata("content", **{field: value}))


def test_skill_constraints_only_promote_optional_context_and_narrow_tools():
    recipe = default_recipe_loader()["pico.turn.v1"]
    metadata = validate_skill_metadata(skill_metadata("content"))

    constrained = apply_skill_constraints(recipe, metadata)

    assert "memory_card" in constrained.required
    assert "memory_card" not in constrained.optional
    assert constrained.allowed_tools == ("read_file",)


def test_skill_constraints_reject_context_or_tools_outside_recipe_authority():
    recipe = default_recipe_loader()["pico.turn.v1"]
    unknown_context = validate_skill_metadata(
        skill_metadata("content", required_context=["secret_context"])
    )
    unknown_tool = validate_skill_metadata(skill_metadata("content", allowed_tools=["unknown_tool"]))

    with pytest.raises(SkillConstraintError):
        apply_skill_constraints(recipe, unknown_context)
    with pytest.raises(SkillConstraintError):
        apply_skill_constraints(recipe, unknown_tool)
