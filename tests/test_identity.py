import pytest

from pico.identity import (
    EnvIdentityProvider,
    IdentityResolutionError,
    StaticIdentityProvider,
    SubjectScope,
    compute_subject_scope_key,
)


def test_static_identity_provider_self_heals_defaults_for_single_workspace_usage(tmp_path):
    provider = StaticIdentityProvider.for_workspace(tmp_path)
    identity = provider.resolve(tmp_path)

    assert identity.scope.tenant_id
    assert identity.scope.assignment_id.startswith("local:")
    assert identity.scope.project_id == str(tmp_path.resolve())
    assert identity.scope.intern_id
    assert identity.scope.mentor_id
    assert identity.actor.actor_id
    assert identity.subject_scope_key.startswith("hmac-sha256:v1:")
    assert "=" not in identity.subject_scope_key


def test_static_identity_provider_reuses_bootstrapped_scope_secret_across_instances(tmp_path):
    first = StaticIdentityProvider.for_workspace(tmp_path).resolve(tmp_path)
    second = StaticIdentityProvider.for_workspace(tmp_path).resolve(tmp_path)

    assert first.subject_scope_key == second.subject_scope_key
    secret_path = tmp_path / ".pico" / "identity" / "scope_secret"
    assert secret_path.exists()
    assert oct(secret_path.stat().st_mode & 0o777) == "0o600"


def test_env_identity_provider_fails_closed_on_any_missing_var(tmp_path, monkeypatch):
    monkeypatch.delenv("PICO_TENANT_ID", raising=False)
    monkeypatch.delenv("PICO_PROJECT_ID", raising=False)
    monkeypatch.delenv("PICO_ACTOR_ID", raising=False)
    monkeypatch.delenv("PICO_MENTOR_ID", raising=False)
    monkeypatch.delenv("PICO_SCOPE_SECRET", raising=False)
    monkeypatch.delenv("PICO_ASSIGNMENT_ID", raising=False)

    provider = EnvIdentityProvider()
    with pytest.raises(IdentityResolutionError):
        provider.resolve(tmp_path)

    monkeypatch.setenv("PICO_TENANT_ID", "tenant-a")
    monkeypatch.setenv("PICO_PROJECT_ID", "proj-a")
    monkeypatch.setenv("PICO_ACTOR_ID", "actor-a")
    monkeypatch.setenv("PICO_MENTOR_ID", "mentor-a")
    with pytest.raises(IdentityResolutionError):
        provider.resolve(tmp_path)  # scope secret still missing

    monkeypatch.setenv("PICO_SCOPE_SECRET", "s3cr3t")
    with pytest.raises(IdentityResolutionError):
        provider.resolve(tmp_path)  # assignment remains mandatory

    monkeypatch.setenv("PICO_ASSIGNMENT_ID", "assignment-a")
    identity = provider.resolve(tmp_path)
    assert identity.scope.tenant_id == "tenant-a"
    assert identity.scope.assignment_id == "assignment-a"
    assert identity.actor.actor_id == "actor-a"


def test_compute_subject_scope_key_is_deterministic_and_scope_specific():
    secret = b"fixed-secret-bytes"
    scope_a = SubjectScope(
        tenant_id="t1", assignment_id="a1", project_id="p1", intern_id="i1", mentor_id="m1"
    )
    scope_b = SubjectScope(
        tenant_id="t1", assignment_id="a1", project_id="p1", intern_id="i2", mentor_id="m1"
    )

    key_a1 = compute_subject_scope_key(secret, scope_a)
    key_a2 = compute_subject_scope_key(secret, scope_a)
    key_b = compute_subject_scope_key(secret, scope_b)

    assert key_a1 == key_a2
    assert key_a1 != key_b


def test_subject_scope_rejects_empty_fields():
    with pytest.raises(IdentityResolutionError):
        SubjectScope(tenant_id="", assignment_id="a1", project_id="p1", intern_id="i1", mentor_id="m1")
