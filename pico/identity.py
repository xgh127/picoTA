"""Actor/subject identity resolution and scope-key computation.

Pico is a single-workspace, single-actor tool today, so there is no real
mentor/intern/tenant service to call. This module gives the rest of the
context system a stable, fail-closed identity boundary to build on: every
`Pico` instance resolves an identity once at construction time, and nothing
downstream may substitute a different scope for missing/mismatched fields.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .workspace import now

DEFAULT_TENANT_ID = "local-tenant"
DEFAULT_MENTOR_ID = "unassigned-mentor"
DEFAULT_ACTOR_ROLE = "developer"
LOCAL_ASSIGNMENT_PREFIX = "local:"
SCOPE_KEY_PREFIX = "hmac-sha256:v1:"
SCOPE_SECRET_RELATIVE_PATH = Path("identity") / "scope_secret"
SCOPE_SECRET_BYTES = 32

ENV_TENANT_ID = "PICO_TENANT_ID"
ENV_ASSIGNMENT_ID = "PICO_ASSIGNMENT_ID"
ENV_PROJECT_ID = "PICO_PROJECT_ID"
ENV_ACTOR_ID = "PICO_ACTOR_ID"
ENV_ACTOR_ROLE = "PICO_ACTOR_ROLE"
ENV_MENTOR_ID = "PICO_MENTOR_ID"
ENV_SCOPE_SECRET = "PICO_SCOPE_SECRET"

ENV_STRICT_REQUIRED = (
    ENV_TENANT_ID,
    ENV_ASSIGNMENT_ID,
    ENV_PROJECT_ID,
    ENV_ACTOR_ID,
    ENV_MENTOR_ID,
    ENV_SCOPE_SECRET,
)


class IdentityResolutionError(RuntimeError):
    """Raised when identity/scope cannot be resolved. Callers must not
    catch-and-fallback: a compile that can't establish scope must fail
    closed rather than guess."""


@dataclass(frozen=True)
class ActorIdentity:
    actor_id: str
    actor_role: str = DEFAULT_ACTOR_ROLE


@dataclass(frozen=True)
class SubjectScope:
    tenant_id: str
    project_id: str
    intern_id: str
    mentor_id: str
    assignment_id: str | None = None
    scope_type: str = "subject"  # "subject" | "organization"

    def __post_init__(self):
        if self.scope_type not in ("subject", "organization"):
            raise ValueError(f"invalid scope_type: {self.scope_type!r}")
        for name in ("tenant_id", "assignment_id", "project_id", "intern_id", "mentor_id"):
            raw_value = getattr(self, name)
            value = "" if raw_value is None else str(raw_value).strip()
            if not value:
                raise IdentityResolutionError(f"SubjectScope.{name} must not be empty")
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class ResolvedIdentity:
    actor: ActorIdentity
    scope: SubjectScope
    subject_scope_key: str
    resolved_at: str = field(default_factory=now)


def canonical_scope_json(scope: SubjectScope) -> str:
    """Return the version-1 scope payload using the protocol's fixed order.

    An array is intentional: it makes field order part of the wire contract
    and avoids object-key ordering differences between JSON implementations.
    UTF-8 characters are preserved instead of being rewritten as ``\\u``
    escapes, and insignificant whitespace is omitted.
    """

    return json.dumps(
        [
            scope.tenant_id,
            scope.assignment_id,
            scope.intern_id,
            scope.project_id,
            scope.mentor_id,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def canonical_scope_payload(scope: SubjectScope) -> bytes:
    return canonical_scope_json(scope).encode("utf-8")


def compute_subject_scope_key(scope_secret: bytes, scope: SubjectScope) -> str:
    """Compute the versioned subject-scope HMAC used for isolation.

    The digest is base64url without padding.  This key is only an index,
    audit, and cache namespace; callers must still perform authorization.
    """
    if not scope_secret:
        raise IdentityResolutionError("scope_secret must not be empty")
    digest = hmac.new(bytes(scope_secret), canonical_scope_payload(scope), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return SCOPE_KEY_PREFIX + encoded


def local_assignment_id(project_id: str) -> str:
    """Return a deterministic local-only assignment for the CLI provider."""

    project = str(Path(project_id).resolve())
    digest = hashlib.sha256(project.encode("utf-8")).hexdigest()
    return LOCAL_ASSIGNMENT_PREFIX + digest


def bootstrap_scope_secret(root) -> bytes:
    """Return the workspace's scope secret, generating one on first use.

    Stored at `<root>/.pico/identity/scope_secret`, mode 0o600. This is the
    "trusted, harness-owned" secret material for the HMAC in
    `compute_subject_scope_key` -- it must never come from a prompt, an env
    var an untrusted caller controls, or client-supplied input.
    """
    path = Path(root) / ".pico" / SCOPE_SECRET_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    nofollow = getattr(os, "O_NOFOLLOW", 0)

    def read_existing():
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | nofollow)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise IdentityResolutionError("workspace scope secret is not a regular file")
            data = os.read(descriptor, SCOPE_SECRET_BYTES + 1)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        if len(data) != SCOPE_SECRET_BYTES:
            raise IdentityResolutionError("workspace scope secret has an invalid length")
        return data

    try:
        return read_existing()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise IdentityResolutionError("workspace scope secret is not a safe regular file") from exc

    secret = secrets.token_bytes(SCOPE_SECRET_BYTES)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        # A concurrent Pico process won the bootstrap race; use its complete
        # secret rather than overwriting it with a different scope namespace.
        try:
            return read_existing()
        except OSError as exc:
            raise IdentityResolutionError("workspace scope secret is not safely readable") from exc
    except OSError as exc:
        raise IdentityResolutionError("could not create the workspace scope secret") from exc
    try:
        view = memoryview(secret)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return secret


class IdentityProvider:
    def resolve(self, root=None) -> ResolvedIdentity:
        raise NotImplementedError


class StaticIdentityProvider(IdentityProvider):
    """Dev-friendly default for Pico's actual single-workspace CLI usage.

    Every field self-heals to a sane default so a bare `Pico(...)` call
    keeps working exactly as before. `mentor_id` is a deliberately inert,
    structurally-present field: Pico has no mentor entity, but the HMAC
    formula requires the field, so it defaults to a stable placeholder
    rather than fabricating fake business meaning.
    """

    def __init__(
        self,
        tenant_id,
        project_id,
        intern_id,
        mentor_id,
        actor_id,
        actor_role,
        scope_secret: bytes,
        assignment_id=None,
    ):
        self.tenant_id = str(tenant_id).strip()
        self.project_id = str(project_id).strip()
        self.intern_id = str(intern_id).strip()
        self.mentor_id = str(mentor_id).strip()
        self.assignment_id = str(assignment_id or local_assignment_id(self.project_id)).strip()
        self.actor_id = str(actor_id).strip()
        self.actor_role = str(actor_role).strip() or DEFAULT_ACTOR_ROLE
        self.scope_secret = bytes(scope_secret)
        for name in ("tenant_id", "assignment_id", "project_id", "intern_id", "mentor_id", "actor_id"):
            if not getattr(self, name):
                raise IdentityResolutionError(f"StaticIdentityProvider.{name} must not be empty")
        if not self.scope_secret:
            raise IdentityResolutionError("StaticIdentityProvider.scope_secret must not be empty")

    @classmethod
    def for_workspace(cls, root, overrides=None, scope_secret=None):
        overrides = dict(overrides or {})
        actor_id = str(
            overrides.get("actor_id")
            or os.environ.get(ENV_ACTOR_ID)
            or os.environ.get("USER")
            or os.environ.get("USERNAME")
            or "local-user"
        ).strip()
        secret = scope_secret if scope_secret is not None else bootstrap_scope_secret(root)
        assignment_id = (
            overrides.get("assignment_id")
            or os.environ.get(ENV_ASSIGNMENT_ID)
            or local_assignment_id(root)
        )
        return cls(
            tenant_id=overrides.get("tenant_id") or os.environ.get(ENV_TENANT_ID) or DEFAULT_TENANT_ID,
            assignment_id=assignment_id,
            project_id=overrides.get("project_id") or os.environ.get(ENV_PROJECT_ID) or str(Path(root).resolve()),
            intern_id=overrides.get("intern_id") or actor_id,
            mentor_id=overrides.get("mentor_id") or os.environ.get(ENV_MENTOR_ID) or DEFAULT_MENTOR_ID,
            actor_id=actor_id,
            actor_role=overrides.get("actor_role") or os.environ.get(ENV_ACTOR_ROLE) or DEFAULT_ACTOR_ROLE,
            scope_secret=secret,
        )

    def resolve(self, root=None) -> ResolvedIdentity:
        scope = SubjectScope(
            tenant_id=self.tenant_id,
            assignment_id=self.assignment_id,
            project_id=self.project_id,
            intern_id=self.intern_id,
            mentor_id=self.mentor_id,
        )
        actor = ActorIdentity(actor_id=self.actor_id, actor_role=self.actor_role)
        subject_scope_key = compute_subject_scope_key(self.scope_secret, scope)
        return ResolvedIdentity(actor=actor, scope=scope, subject_scope_key=subject_scope_key)


class EnvIdentityProvider(IdentityProvider):
    """Strict variant: every field must come from the environment, no
    defaulting. This is the stand-in for "a real, trusted multi-tenant
    identity source" the design doc requires -- wire this (or an equivalent
    that talks to a real identity/project service) when Pico is deployed
    behind an actual multi-actor caller.
    """

    def resolve(self, root=None) -> ResolvedIdentity:
        missing = [name for name in ENV_STRICT_REQUIRED if not os.environ.get(name)]
        if missing:
            raise IdentityResolutionError(f"missing required identity env vars: {', '.join(missing)}")
        scope = SubjectScope(
            tenant_id=os.environ[ENV_TENANT_ID],
            assignment_id=os.environ[ENV_ASSIGNMENT_ID],
            project_id=os.environ[ENV_PROJECT_ID],
            intern_id=os.environ[ENV_ACTOR_ID],
            mentor_id=os.environ[ENV_MENTOR_ID],
        )
        actor = ActorIdentity(
            actor_id=os.environ[ENV_ACTOR_ID],
            actor_role=os.environ.get(ENV_ACTOR_ROLE, DEFAULT_ACTOR_ROLE),
        )
        secret = os.environ[ENV_SCOPE_SECRET].encode("utf-8")
        subject_scope_key = compute_subject_scope_key(secret, scope)
        return ResolvedIdentity(actor=actor, scope=scope, subject_scope_key=subject_scope_key)
