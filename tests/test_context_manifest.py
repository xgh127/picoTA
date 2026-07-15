from pico.context.manifest import BlockRef, build_manifest


def test_build_manifest_shape():
    manifest = build_manifest(
        request_id="req-1",
        subject_scope_key="sha256:abc",
        recipe_id="pico.turn.v1",
        policy_version=1,
        state_version=2,
        cache_meta={"fingerprint": "f1", "prefix_tokens": 42, "status": "hit"},
        included=[BlockRef(block_id="b1", reason="required_by_recipe")],
        excluded=[BlockRef(block_id="b2", reason="expired")],
        tokens={"estimated": 500, "budget": 3000},
    )

    data = manifest.to_dict()
    assert data["request_id"] == "req-1"
    assert data["subject_scope_key"] == "sha256:abc"
    assert data["recipe_id"] == "pico.turn.v1"
    assert data["policy_version"] == 1
    assert data["state_version"] == 2
    assert data["cache"] == {"fingerprint": "f1", "prefix_tokens": 42, "status": "hit"}
    assert data["included"] == [{"block_id": "b1", "reason": "required_by_recipe"}]
    assert data["excluded"] == [{"block_id": "b2", "reason": "expired"}]
    assert data["tokens"] == {"estimated": 500, "budget": 3000}
    assert data["created_at"]
