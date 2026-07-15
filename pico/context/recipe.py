"""Recipe: versioned, declarative context-loading config.

Replaces the idea of a hardcoded section list with a small, validated YAML
document that says what a given compile step must/may/must-not load, and
what evidence it requires for high-impact decisions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Iterable

import yaml

RECIPE_ID_PATTERN = re.compile(r"^(?P<name>.+)\.v(?P<version>\d+)$")
CONFIG_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")

DEFAULT_MEMORY_LIMIT = 5
MAX_MEMORY_LIMIT = 5
PERSIST_BEFORE_LOSS = "persist_before_loss"
ALLOW_SMALL_NON_EVIDENCE_PROJECTION = "allow_small_non_evidence_projection"
PERSISTENCE_POLICIES = (
    PERSIST_BEFORE_LOSS,
    ALLOW_SMALL_NON_EVIDENCE_PROJECTION,
)

REQUIRED_TOP_LEVEL_KEYS = ("recipe_id", "required")
OPTIONAL_TOP_LEVEL_KEYS = (
    "name",
    "optional",
    "forbidden",
    "budgets",
    "evidence_required_for",
    "output_schema",
    "allowed_tools",
    "allow_organization_memory",
    "memory_limit",
    "persistence_policy",
)
ALL_TOP_LEVEL_KEYS = set(REQUIRED_TOP_LEVEL_KEYS) | set(OPTIONAL_TOP_LEVEL_KEYS)


class RecipeValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Recipe:
    recipe_id: str
    name: str
    version: int
    required: tuple
    optional: tuple
    forbidden: tuple
    budgets: dict
    evidence_required_for: tuple
    output_schema: str
    allowed_tools: tuple = ()
    allow_organization_memory: bool = False
    memory_limit: int = DEFAULT_MEMORY_LIMIT
    persistence_policy: str = PERSIST_BEFORE_LOSS

    def block_types_allowed(self):
        return set(self.required) | set(self.optional)

    def is_forbidden(self, block_type):
        return block_type in self.forbidden


def parse_recipe_id(recipe_id: str):
    if not isinstance(recipe_id, str):
        raise RecipeValidationError(f"recipe_id must be a string: {recipe_id!r}")
    match = RECIPE_ID_PATTERN.match(recipe_id)
    if not match:
        raise RecipeValidationError(f"recipe_id must end in '.vN': {recipe_id!r}")
    if not CONFIG_NAME_PATTERN.fullmatch(match.group("name")):
        raise RecipeValidationError(f"recipe_id contains an invalid name: {recipe_id!r}")
    return match.group("name"), int(match.group("version"))


def _string_list(data: dict, key: str, *, required=False) -> list[str]:
    if key not in data:
        if required:
            raise RecipeValidationError(f"recipe is missing required key: {key}")
        return []
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise RecipeValidationError(f"{key} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise RecipeValidationError(f"{key} must not contain duplicates")
    return value


def _validate_config_names(values: Iterable[str], field_name: str) -> None:
    invalid = [value for value in values if not CONFIG_NAME_PATTERN.fullmatch(value)]
    if invalid:
        raise RecipeValidationError(f"{field_name} contains invalid names: {invalid!r}")


def validate_recipe(data: dict) -> None:
    if not isinstance(data, dict):
        raise RecipeValidationError("recipe document must be a mapping")
    missing = [key for key in REQUIRED_TOP_LEVEL_KEYS if key not in data]
    if missing:
        raise RecipeValidationError(f"recipe is missing required keys: {', '.join(missing)}")
    unknown = set(data) - ALL_TOP_LEVEL_KEYS
    if unknown:
        raise RecipeValidationError(f"recipe has unknown keys: {', '.join(sorted(unknown))}")

    parse_recipe_id(data["recipe_id"])
    name = data.get("name")
    if name is not None and (not isinstance(name, str) or not CONFIG_NAME_PATTERN.fullmatch(name)):
        raise RecipeValidationError(f"invalid recipe name: {name!r}")

    required = _string_list(data, "required", required=True)
    optional = _string_list(data, "optional")
    forbidden = _string_list(data, "forbidden")
    if not required:
        raise RecipeValidationError("recipe must declare at least one required block type")
    _validate_config_names(required + optional + forbidden, "context block lists")

    required_set, optional_set, forbidden_set = set(required), set(optional), set(forbidden)
    overlap = (required_set & optional_set) | (required_set & forbidden_set) | (optional_set & forbidden_set)
    if overlap:
        raise RecipeValidationError(f"required/optional/forbidden must be disjoint, overlap: {sorted(overlap)}")

    budgets = data.get("budgets", {})
    if not isinstance(budgets, dict):
        raise RecipeValidationError("budgets must be a mapping")
    for key, value in budgets.items():
        if not isinstance(key, str) or not CONFIG_NAME_PATTERN.fullmatch(key):
            raise RecipeValidationError(f"invalid budgets key: {key!r}")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise RecipeValidationError(f"budgets[{key!r}] must be a positive int")

    evidence_required_for = _string_list(data, "evidence_required_for")
    _validate_config_names(evidence_required_for, "evidence_required_for")

    output_schema = data.get("output_schema", "")
    if not isinstance(output_schema, str):
        raise RecipeValidationError("output_schema must be a string")
    if output_schema and not CONFIG_NAME_PATTERN.fullmatch(output_schema):
        raise RecipeValidationError(f"invalid output_schema: {output_schema!r}")

    allowed_tools = _string_list(data, "allowed_tools")
    _validate_config_names(allowed_tools, "allowed_tools")

    allow_organization_memory = data.get("allow_organization_memory", False)
    if not isinstance(allow_organization_memory, bool):
        raise RecipeValidationError("allow_organization_memory must be a boolean")

    memory_limit = data.get("memory_limit", DEFAULT_MEMORY_LIMIT)
    if (
        not isinstance(memory_limit, int)
        or isinstance(memory_limit, bool)
        or not 0 <= memory_limit <= MAX_MEMORY_LIMIT
    ):
        raise RecipeValidationError(f"memory_limit must be an int in [0, {MAX_MEMORY_LIMIT}]")

    persistence_policy = data.get("persistence_policy", PERSIST_BEFORE_LOSS)
    if persistence_policy not in PERSISTENCE_POLICIES:
        raise RecipeValidationError(
            f"persistence_policy must be one of {PERSISTENCE_POLICIES!r}"
        )


def _build_recipe(data: dict) -> Recipe:
    validate_recipe(data)
    name, version = parse_recipe_id(data["recipe_id"])
    return Recipe(
        recipe_id=data["recipe_id"],
        name=data.get("name", name),
        version=version,
        required=tuple(data.get("required") or []),
        optional=tuple(data.get("optional") or []),
        forbidden=tuple(data.get("forbidden") or []),
        budgets=dict(data.get("budgets") or {}),
        evidence_required_for=tuple(data.get("evidence_required_for") or []),
        output_schema=str(data.get("output_schema", "")),
        allowed_tools=tuple(data.get("allowed_tools") or []),
        allow_organization_memory=data.get("allow_organization_memory", False),
        memory_limit=data.get("memory_limit", DEFAULT_MEMORY_LIMIT),
        persistence_policy=data.get("persistence_policy", PERSIST_BEFORE_LOSS),
    )


def load_recipe(path) -> Recipe:
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return _build_recipe(data)


def load_recipe_dir(dir_path) -> dict:
    recipes = {}
    for path in sorted(Path(dir_path).glob("*.yaml")):
        recipe = load_recipe(path)
        if recipe.recipe_id in recipes:
            raise RecipeValidationError(f"duplicate recipe_id: {recipe.recipe_id}")
        recipes[recipe.recipe_id] = recipe
    return recipes


def _load_package_recipe_directory(*parts: str) -> dict:
    recipes = {}
    package_files = resources.files("pico.context").joinpath("recipes", *parts)
    for entry in sorted(package_files.iterdir(), key=lambda item: item.name):
        if entry.name.endswith(".yaml"):
            data = yaml.safe_load(entry.read_text(encoding="utf-8"))
            recipe = _build_recipe(data)
            if recipe.recipe_id in recipes:
                raise RecipeValidationError(f"duplicate recipe_id: {recipe.recipe_id}")
            recipes[recipe.recipe_id] = recipe
    return recipes


def default_recipe_loader() -> dict:
    """Load the three legacy Pico recipes.

    This compatibility loader intentionally does not recurse into the
    ``assistant`` recipe directory. New callers that need the full catalog
    should use :func:`all_recipe_loader`.
    """
    return _load_package_recipe_directory()


def assistant_recipe_loader() -> dict:
    """Load the seven intern-assistant business-step recipes."""
    return _load_package_recipe_directory("assistant")


def all_recipe_loader() -> dict:
    """Load the complete recipe catalog and reject duplicate IDs."""
    recipes = default_recipe_loader()
    for recipe_id, recipe in assistant_recipe_loader().items():
        if recipe_id in recipes:
            raise RecipeValidationError(f"duplicate recipe_id: {recipe_id}")
        recipes[recipe_id] = recipe
    return recipes
