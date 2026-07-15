import pytest

from pico.context.recipe import RecipeValidationError, default_recipe_loader, load_recipe, parse_recipe_id, validate_recipe


def test_default_recipe_loader_loads_the_three_shipped_recipes():
    recipes = default_recipe_loader()

    assert set(recipes) == {"pico.turn.v1", "pico.delegate.v1", "pico.compact.v1"}
    turn_recipe = recipes["pico.turn.v1"]
    assert turn_recipe.version == 1
    assert "stable_prefix" in turn_recipe.required
    assert "memory_card" in turn_recipe.optional
    assert turn_recipe.evidence_required_for == ("durable_memory_promotion",)


def test_delegate_recipe_forbids_memory_card():
    recipes = default_recipe_loader()
    delegate_recipe = recipes["pico.delegate.v1"]

    assert "memory_card" in delegate_recipe.forbidden
    assert "memory_card" not in delegate_recipe.block_types_allowed()


def test_required_optional_forbidden_must_be_disjoint():
    with pytest.raises(RecipeValidationError):
        validate_recipe(
            {
                "recipe_id": "bad.v1",
                "required": ["stable_prefix"],
                "optional": ["stable_prefix"],
            }
        )


def test_malformed_recipe_id_raises():
    with pytest.raises(RecipeValidationError):
        parse_recipe_id("no-version-suffix")


def test_missing_required_keys_raises():
    with pytest.raises(RecipeValidationError):
        validate_recipe({"recipe_id": "x.v1"})


def test_unknown_recipe_id_lookup_raises_keyerror():
    recipes = default_recipe_loader()
    with pytest.raises(KeyError):
        recipes["does.not.exist.v1"]


def test_load_recipe_from_explicit_path(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text(
        "recipe_id: custom.v3\n"
        "required: [stable_prefix]\n"
        "budgets:\n  stable_prefix: 100\n",
        encoding="utf-8",
    )
    recipe = load_recipe(path)
    assert recipe.recipe_id == "custom.v3"
    assert recipe.version == 3
    assert recipe.budgets == {"stable_prefix": 100}
