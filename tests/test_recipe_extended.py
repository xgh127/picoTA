import pytest

from pico.context.recipe import (
    ALLOW_SMALL_NON_EVIDENCE_PROJECTION,
    DEFAULT_MEMORY_LIMIT,
    PERSIST_BEFORE_LOSS,
    RecipeValidationError,
    all_recipe_loader,
    assistant_recipe_loader,
    load_recipe,
    validate_recipe,
)


ASSISTANT_RECIPE_IDS = {
    "daily_report.collect.v1",
    "daily_report.confirm_progress.v1",
    "next_day.plan.v1",
    "plan.check_deviation.v1",
    "phase_plan.adjust.v1",
    "phase.review.v1",
    "escalation.evaluate.v1",
}


def test_legacy_recipe_without_new_fields_gets_safe_defaults(tmp_path):
    path = tmp_path / "legacy.yaml"
    path.write_text("recipe_id: legacy.v1\nrequired: [runtime]\n", encoding="utf-8")

    recipe = load_recipe(path)

    assert recipe.allowed_tools == ()
    assert recipe.allow_organization_memory is False
    assert recipe.memory_limit == DEFAULT_MEMORY_LIMIT
    assert recipe.persistence_policy == PERSIST_BEFORE_LOSS


def test_recipe_accepts_explicit_safe_extension_fields(tmp_path):
    path = tmp_path / "extended.yaml"
    path.write_text(
        "recipe_id: extended.v2\n"
        "required: [runtime]\n"
        "allowed_tools: [read_file]\n"
        "allow_organization_memory: true\n"
        "memory_limit: 2\n"
        f"persistence_policy: {ALLOW_SMALL_NON_EVIDENCE_PROJECTION}\n",
        encoding="utf-8",
    )

    recipe = load_recipe(path)

    assert recipe.allowed_tools == ("read_file",)
    assert recipe.allow_organization_memory is True
    assert recipe.memory_limit == 2
    assert recipe.persistence_policy == ALLOW_SMALL_NON_EVIDENCE_PROJECTION


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allowed_tools", "read_file"),
        ("allowed_tools", ["read_file", "read_file"]),
        ("allowed_tools", ["Read File"]),
        ("allow_organization_memory", 1),
        ("memory_limit", True),
        ("memory_limit", -1),
        ("memory_limit", 6),
        ("persistence_policy", "drop_before_persist"),
    ],
)
def test_recipe_extension_fields_are_strictly_validated(field, value):
    data = {"recipe_id": "strict.v1", "required": ["runtime"], field: value}

    with pytest.raises(RecipeValidationError):
        validate_recipe(data)


def test_assistant_loader_exposes_exactly_the_seven_business_steps():
    recipes = assistant_recipe_loader()

    assert set(recipes) == ASSISTANT_RECIPE_IDS
    for recipe in recipes.values():
        assert set(recipe.required).isdisjoint(recipe.optional)
        assert set(recipe.required).isdisjoint(recipe.forbidden)
        assert set(recipe.optional).isdisjoint(recipe.forbidden)
        assert recipe.allow_organization_memory is False
        assert recipe.persistence_policy == PERSIST_BEFORE_LOSS


def test_assistant_recipes_encode_the_design_specific_limits():
    recipes = assistant_recipe_loader()

    assert recipes["daily_report.collect.v1"].memory_limit == 0
    assert recipes["next_day.plan.v1"].memory_limit == 3
    assert recipes["next_day.plan.v1"].optional == ("profile_capability",)
    assert "task_status_change" in recipes["daily_report.confirm_progress.v1"].evidence_required_for
    assert "critical_plan_adjustment" in recipes["phase_plan.adjust.v1"].evidence_required_for
    assert "escalation" in recipes["escalation.evaluate.v1"].evidence_required_for


def test_all_recipe_loader_combines_legacy_and_assistant_catalogs():
    recipes = all_recipe_loader()

    assert set(recipes) == ASSISTANT_RECIPE_IDS | {
        "pico.turn.v1",
        "pico.delegate.v1",
        "pico.compact.v1",
    }
