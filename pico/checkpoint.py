"""Checkpoint and resume-state helpers."""

import uuid
from copy import deepcopy

from .context.cache import stable_hash
from .context.evidence import EvidenceClaim, EvidenceRequirementError, new_capsule
from .features import memory as memorylib
from .workspace import clip, now

CHECKPOINT_SCHEMA_VERSION = "phase1-v1"
CHECKPOINT_NONE_STATUS = "no-checkpoint"
CHECKPOINT_FULL_VALID_STATUS = "full-valid"
CHECKPOINT_PARTIAL_STALE_STATUS = "partial-stale"
CHECKPOINT_WORKSPACE_MISMATCH_STATUS = "workspace-mismatch"
CHECKPOINT_SCHEMA_MISMATCH_STATUS = "schema-mismatch"

RUNTIME_IDENTITY_KEYS = (
    "subject_scope_key",
    "tenant_id",
    "assignment_id",
    "project_id",
    "intern_id",
    "mentor_id",
    "cwd",
    "model",
    "model_client",
    "approval_policy",
    "read_only",
    "max_steps",
    "max_new_tokens",
    "feature_flags",
    "shell_env_allowlist",
    "workspace_fingerprint",
    "tool_signature",
)


def current_runtime_identity(agent):
    scope = agent.resolved_identity.scope
    return {
        "session_id": agent.session.get("id", ""),
        "subject_scope_key": agent.subject_scope_key,
        "tenant_id": scope.tenant_id,
        "assignment_id": scope.assignment_id,
        "project_id": scope.project_id,
        "intern_id": scope.intern_id,
        "mentor_id": scope.mentor_id,
        "cwd": str(agent.root),
        "model": str(getattr(agent.model_client, "model", "")),
        "model_client": agent.model_client.__class__.__name__,
        "approval_policy": agent.approval_policy,
        "read_only": bool(agent.read_only),
        "max_steps": int(agent.max_steps),
        "max_new_tokens": int(agent.max_new_tokens),
        "feature_flags": dict(agent.feature_flags),
        "shell_env_allowlist": list(agent.shell_env_allowlist),
        "workspace_fingerprint": getattr(getattr(agent, "prefix_state", None), "workspace_fingerprint", agent.workspace.fingerprint()),
        "tool_signature": agent.tool_signature(),
    }


def checkpoint_state(agent):
    agent._ensure_session_shape()
    return agent.session["checkpoints"]


def current_checkpoint(agent):
    state = checkpoint_state(agent)
    checkpoint_id = str(state.get("current_id", "")).strip()
    if not checkpoint_id:
        return None
    return state.get("items", {}).get(checkpoint_id)


def evaluate_resume_state(agent):
    previous_resume_state = dict(agent.session.get("resume_state", {}) or {})
    invalidated = agent.invalidate_stale_memory()
    checkpoint = current_checkpoint(agent)
    status = CHECKPOINT_NONE_STATUS
    stale_paths = list(invalidated)
    mismatch_fields = []
    if checkpoint:
        if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            status = CHECKPOINT_SCHEMA_MISMATCH_STATUS
        else:
            for item in checkpoint.get("key_files", []):
                path = str(item.get("path", "")).strip()
                if not path:
                    continue
                expected = item.get("freshness")
                current = memorylib.file_freshness(path, agent.root)
                if expected != current and path not in stale_paths:
                    stale_paths.append(path)
            saved_identity = dict(checkpoint.get("runtime_identity", {}) or agent.session.get("runtime_identity", {}) or {})
            current_identity = current_runtime_identity(agent)
            for key in RUNTIME_IDENTITY_KEYS:
                if key not in saved_identity:
                    continue
                if saved_identity.get(key) != current_identity.get(key):
                    mismatch_fields.append(key)
            mismatch_fields.sort()
            if stale_paths:
                status = CHECKPOINT_PARTIAL_STALE_STATUS
            elif mismatch_fields:
                status = CHECKPOINT_WORKSPACE_MISMATCH_STATUS
            else:
                status = CHECKPOINT_FULL_VALID_STATUS

    resume_state = {
        "status": status,
        "stale_paths": stale_paths,
        "runtime_identity_mismatch_fields": mismatch_fields,
        "stale_summary_invalidations": max(
            len(invalidated),
            int(previous_resume_state.get("stale_summary_invalidations", 0))
            if status == CHECKPOINT_PARTIAL_STALE_STATUS
            else 0,
        ),
    }
    agent.session["resume_state"] = resume_state
    agent.session["runtime_identity"] = current_runtime_identity(agent)
    return resume_state


def render_checkpoint_text(agent):
    checkpoint = current_checkpoint(agent)
    if not checkpoint:
        return ""
    lines = [
        "Task checkpoint:",
        f"- Resume status: {agent.resume_state.get('status', CHECKPOINT_NONE_STATUS)}",
        f"- Current goal: {checkpoint.get('current_goal', '-') or '-'}",
        f"- Current blocker: {checkpoint.get('current_blocker', '-') or '-'}",
        f"- Next step: {checkpoint.get('next_step', '-') or '-'}",
    ]
    key_files = [str(item.get("path", "")).strip() for item in checkpoint.get("key_files", []) if str(item.get("path", "")).strip()]
    lines.append(f"- Key files: {', '.join(key_files) or '-'}")
    if checkpoint.get("completed"):
        lines.append("- Completed: " + " | ".join(str(item) for item in checkpoint.get("completed", [])))
    if checkpoint.get("excluded"):
        lines.append("- Excluded: " + " | ".join(str(item) for item in checkpoint.get("excluded", [])))
    if agent.resume_state.get("stale_paths"):
        lines.append("- Stale paths: " + ", ".join(agent.resume_state["stale_paths"]))
    if checkpoint.get("active_deviations"):
        deviations = "; ".join(f"{item.get('type', '')}:{item.get('subject_id', '')}" for item in checkpoint["active_deviations"])
        lines.append(f"- Active deviations: {deviations}")
    if checkpoint.get("open_loops"):
        lines.append("- Open loops: " + " | ".join(str(item) for item in checkpoint["open_loops"]))
    summary = str(checkpoint.get("summary", "")).strip()
    if summary:
        lines.append(f"- Summary: {summary}")
    return "\n".join(lines)


def infer_next_step(task_state):
    if task_state.status == "completed":
        return "No next step recorded."
    if task_state.stop_reason == "step_limit_reached":
        return "Resume from the latest checkpoint and continue the task."
    if task_state.last_tool:
        return f"Decide the next action after {task_state.last_tool}."
    return "Continue the task from the latest checkpoint."


def _events_since_cursor(agent, current):
    history = list(agent.session.get("history", []))
    cursor = int((current or {}).get("history_cursor", 0))
    cursor = max(0, min(cursor, len(history)))
    return history[cursor:], len(history), cursor


def _confirmed_facts_and_deviations(new_events, *, session_id, start_index):
    confirmed_facts = []
    active_deviations = []
    for index, item in enumerate(new_events):
        if item.get("role") != "tool":
            continue
        name = str(item.get("name", ""))
        status = str(item.get("tool_status", "")) or "ok"
        ref = (
            f"tool_event:{session_id}:{start_index + index}@"
            f"{stable_hash(dict(item))}"
        )
        if status == "ok":
            path = str(item.get("args", {}).get("path", "")).strip()
            subject = path or name
            confirmed_facts.append({"claim": f"{name} succeeded on {subject}", "source_refs": [ref]})
        else:
            path = str(item.get("args", {}).get("path", "")).strip()
            deviation_type = "tool_partial_success" if status == "partial_success" else "tool_failure"
            active_deviations.append({"type": deviation_type, "subject_id": path or name, "detail": clip(str(item.get("content", "")), 120)})
    return confirmed_facts, active_deviations


def _plan_baseline(agent):
    # Pico has no milestone/phase concept, so the pragmatic analog of a
    # "plan baseline" is the tool/policy contract this turn's plan was made
    # under -- if that contract changes mid-task, whatever the checkpoint's
    # next_step assumed may no longer hold.
    return {
        "tool_signature": agent.tool_signature(),
        "feature_flags": dict(agent.feature_flags),
        "recipe_id": getattr(agent, "default_recipe_id", ""),
    }


def _stable_merge_checkpoint_items(*values):
    """Merge checkpoint arrays without reordering or hashability assumptions."""
    merged = []
    for value in values:
        if not isinstance(value, (list, tuple)):
            continue
        for item in value:
            if item not in merged:
                merged.append(deepcopy(item))
    return merged


def _checkpoint_plan_baseline(agent, current):
    baseline = (current or {}).get("plan_baseline", {})
    inherited = deepcopy(baseline) if isinstance(baseline, dict) else {}
    # Runtime-derived values are authoritative for the new checkpoint, while
    # business versions (roadmap/plan/phase) survive if this runtime does not
    # own a newer value for them.
    inherited.update(_plan_baseline(agent))
    return inherited


def _checkpoint_artifacts(agent, current):
    """Carry live Artifact refs forward and add newly persisted outputs.

    Checkpoints are deltas, but Artifact references are durable recovery
    state.  A later checkpoint must therefore inherit them until an explicit
    close/retention policy removes them; merely creating another checkpoint
    is not such a policy boundary.
    """
    inherited = (current or {}).get("artifacts", [])
    if not isinstance(inherited, list):
        inherited = []
    pending = list(getattr(agent, "pending_artifact_ids", []))
    return _stable_merge_checkpoint_items(inherited, pending)


def _task_completion_evidence_capsule_id(agent, task_state):
    # Deliberately keyed off the whole-run task_state counters rather than
    # inferring completion solely from checkpoint facts: checkpoints carry
    # prior semantic state forward, while these counters identify whether
    # this run itself produced tool verification.
    verification = "tool_verified" if task_state.tool_steps > 0 else "unverified"
    evidence_refs = (
        [
            f"tool_run:{task_state.run_id}:{task_state.last_tool}@"
            f"{stable_hash({'task_id': task_state.task_id, 'tool': task_state.last_tool})}"
        ]
        if task_state.last_tool
        else []
    )
    claim = EvidenceClaim(field="status", proposed_value="completed", evidence_refs=evidence_refs, verification=verification)
    missing_evidence = [] if verification == "tool_verified" else ["acceptance_test_result"]
    capsule = new_capsule(
        agent.subject_scope_key,
        "task_completion_checkpoint",
        subject_id=task_state.task_id,
        claims=[claim],
        missing_evidence=missing_evidence,
    )
    if agent.feature_enabled("strict_evidence_gate") and not claim.evidence_refs:
        raise EvidenceRequirementError("task_completion_checkpoint requires evidence_refs under strict_evidence_gate")
    agent.context_store.insert_evidence_capsule(capsule)
    return capsule.capsule_id


def create_checkpoint(agent, task_state, user_message, trigger):
    state = checkpoint_state(agent)
    current = current_checkpoint(agent)
    checkpoint_id = "ckpt_" + uuid.uuid4().hex[:8]
    key_files = []
    freshness = {}
    for path in agent.memory.to_dict()["working"]["recent_files"]:
        file_freshness = memorylib.file_freshness(path, agent.root)
        freshness[path] = file_freshness
        key_files.append({"path": path, "freshness": file_freshness})

    new_events, history_cursor, history_start = _events_since_cursor(agent, current)
    confirmed_facts, active_deviations = _confirmed_facts_and_deviations(
        new_events,
        session_id=str(agent.session.get("id", "")),
        start_index=history_start,
    )
    current_blocker = "" if str(task_state.stop_reason or "") in ("", "final_answer_returned") else str(task_state.stop_reason)
    next_step = infer_next_step(task_state)
    open_loops = ([current_blocker] if current_blocker else []) + ([next_step] if next_step else [])

    evidence_capsule_ids = list(getattr(agent, "last_evidence_capsule_ids", []))
    if trigger == "run_finished" and task_state.status == "completed":
        evidence_capsule_ids.append(_task_completion_evidence_capsule_id(agent, task_state))

    confirmed_facts = _stable_merge_checkpoint_items(
        (current or {}).get("confirmed_facts", []),
        confirmed_facts,
    )
    active_deviations = _stable_merge_checkpoint_items(
        (current or {}).get("active_deviations", []),
        active_deviations,
    )
    open_loops = _stable_merge_checkpoint_items(
        (current or {}).get("open_loops", []),
        open_loops,
    )
    next_actions = _stable_merge_checkpoint_items(
        (current or {}).get("next_actions", []),
        [next_step] if next_step else [],
    )
    evidence_capsule_ids = _stable_merge_checkpoint_items(
        (current or {}).get("evidence_capsules", []),
        evidence_capsule_ids,
    )

    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "scope_type": "subject",
        "scope_key": agent.subject_scope_key,
        "session_id": str(agent.session.get("id", "")),
        "parent_checkpoint_id": current.get("checkpoint_id", "") if current else "",
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "created_at": now(),
        "current_goal": str(user_message),
        "completed": [task_state.final_answer] if task_state.final_answer else [],
        "excluded": [],
        "current_blocker": current_blocker,
        "next_step": next_step,
        "key_files": key_files,
        "freshness": freshness,
        "summary": f"{trigger}: {clip(str(user_message), 120)}",
        "runtime_identity": current_runtime_identity(agent),
        "history_cursor": history_cursor,
        "plan_baseline": _checkpoint_plan_baseline(agent, current),
        "confirmed_facts": confirmed_facts,
        "active_deviations": active_deviations,
        "open_loops": open_loops,
        "artifacts": _checkpoint_artifacts(agent, current),
        "next_actions": next_actions,
        "evidence_capsules": evidence_capsule_ids,
        "decisions": _stable_merge_checkpoint_items(
            (current or {}).get("decisions", []),
        ),
        "user_constraints": _stable_merge_checkpoint_items(
            (current or {}).get("user_constraints", []),
        ),
        "uncertain_items": _stable_merge_checkpoint_items(
            (current or {}).get("uncertain_items", []),
        ),
    }
    state["items"][checkpoint_id] = checkpoint
    state["current_id"] = checkpoint_id
    task_state.checkpoint_id = checkpoint_id
    agent.session["runtime_identity"] = checkpoint["runtime_identity"]
    agent.session_path = agent.session_store.save(agent.session)
    agent.context_store.insert_checkpoint_row(checkpoint_id, agent.subject_scope_key, agent.session.get("id", ""), checkpoint)
    # Pending refs are cleared only after the durable checkpoint and scoped
    # index both contain them.  They remain inherited by future checkpoints.
    agent.pending_artifact_ids = []
    return checkpoint
