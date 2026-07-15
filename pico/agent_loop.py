"""Agent control loop extracted from the runtime facade."""

import time

from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .context.compression import CompactionCheckpointError, CompactionReferenceError
from .context.reactive_compact import ReactiveCompactExhaustedError, is_prompt_too_long_error, reactive_compact
from .context.transcript import TranscriptPersistenceError
from .task_state import TaskState
from .workspace import clip, now


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent

    def _stop_after_prompt_too_long(self, task_state, user_message, run_started_at):
        agent = self.agent
        final = "Stopped after the model repeatedly rejected the prompt as too long."
        task_state.stop_model_error(final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        return final

    def run(self, user_message):
        agent = self.agent
        run_started_at = time.monotonic()
        agent.memory.set_task_summary(user_message)
        agent.record({"role": "user", "content": user_message, "created_at": now()})

        task_state = TaskState.create(run_id=agent.new_run_id(), task_id=agent.new_task_id(), user_request=user_message)
        task_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        agent.current_task_state = task_state
        agent.current_run_dir = agent.run_store.start_run(task_state)
        agent.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )

        tool_steps = 0
        attempts = 0
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while tool_steps < agent.max_steps and attempts < max_attempts:
            attempts += 1
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )
            checkpoint = None
            if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="freshness_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
            elif prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                agent.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="workspace_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            if prompt_metadata.get("budget_reductions"):
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="context_reduction")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            if agent.context_compaction_required(prompt_metadata):
                budget = dict(prompt_metadata.get("dynamic_budget", {}) or {})
                input_tokens = int(prompt_metadata.get("input_tokens", 0) or 0)
                hard_trigger = int(budget.get("hard_trigger", 0) or 0)
                trigger = "hard_trigger" if hard_trigger > 0 and input_tokens > hard_trigger else "context_reduction"
                if checkpoint is None:
                    checkpoint = agent.create_checkpoint(task_state, user_message, trigger=trigger)
                    agent.run_store.write_task_state(task_state)
                    agent.emit_trace(
                        task_state,
                        "checkpoint_created",
                        {
                            "checkpoint_id": checkpoint["checkpoint_id"],
                            "trigger": trigger,
                        },
                    )
                history_before = len(agent.session.get("history", []))
                try:
                    compacted = agent.compact_active_context(
                        trigger,
                        agent._compaction_target_tokens(prompt_metadata),
                    )
                except (CompactionCheckpointError, CompactionReferenceError, TranscriptPersistenceError) as exc:
                    # The original prompt and history are still intact because
                    # every lossy mutation happens only after C0 succeeds.
                    agent.emit_trace(
                        task_state,
                        "session_compaction_skipped",
                        {"trigger": trigger, "error": clip(str(exc), 300)},
                    )
                else:
                    agent.run_store.write_task_state(task_state)
                    agent.emit_trace(
                        task_state,
                        "session_compacted",
                        {
                            "checkpoint_id": compacted.checkpoint_id,
                            "transcript_id": compacted.transcript_id,
                            "trigger": trigger,
                            "tokens_before": compacted.tokens_before,
                            "tokens_after": compacted.tokens_after,
                            "history_before": history_before,
                            "history_after": len(agent.session.get("history", [])),
                            "mode": compacted.rehydrated_context.get("compaction_mode", "delta"),
                            "fallback_reason": compacted.rehydrated_context.get("fallback_reason", ""),
                        },
                    )
                    prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
                    agent.emit_trace(
                        task_state,
                        "prompt_rebuilt_after_compaction",
                        {
                            "prompt_metadata": prompt_metadata,
                            "checkpoint_id": compacted.checkpoint_id,
                        },
                    )
            agent.emit_trace(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            prompt_cache_key = None
            prompt_cache_retention = None
            minimum_cache_tokens = int(
                getattr(agent.model_client, "minimum_cacheable_tokens", 0) or 0
            )
            prefix_tokens = int(
                getattr(getattr(agent, "last_manifest", None), "cache", {}).get(
                    "prefix_tokens", 0
                )
            )
            cache_eligible = prefix_tokens >= minimum_cache_tokens
            prompt_metadata["cache_eligible"] = cache_eligible
            if getattr(agent.model_client, "supports_prompt_cache", False) and cache_eligible:
                # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"
            model_started_at = time.monotonic()
            try:
                raw = agent.model_client.complete(
                    prompt,
                    agent.max_new_tokens,
                    prompt_cache_key=prompt_cache_key,
                    prompt_cache_retention=prompt_cache_retention,
                )
            except Exception as exc:
                if not agent.feature_enabled("reactive_compact") or not is_prompt_too_long_error(exc):
                    raise
                agent.emit_trace(task_state, "reactive_compact_triggered", {"error": clip(str(exc), 300)})
                try:
                    compacted_prompt = reactive_compact(agent, task_state, user_message)
                    raw = agent.model_client.complete(compacted_prompt, agent.max_new_tokens)
                except ReactiveCompactExhaustedError:
                    return self._stop_after_prompt_too_long(task_state, user_message, run_started_at)
                except Exception as retry_exc:
                    if not is_prompt_too_long_error(retry_exc):
                        raise
                    # The one allowed reactive-compact attempt still didn't
                    # fit -- there's nothing cheaper left to try, so stop
                    # gracefully instead of crashing the run.
                    return self._stop_after_prompt_too_long(task_state, user_message, run_started_at)
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            if completion_metadata:
                # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                # 方便统一写入 report 和 trace。
                prompt_metadata.update(completion_metadata)
            agent.update_context_cache_manifest(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            kind, payload = agent.parse(raw)
            agent.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )

            if kind == "tool":
                tool_steps += 1
                name = payload.get("name", "")
                args = payload.get("args", {})
                task_state.record_tool(name)
                tool_started_at = time.monotonic()
                tool_result = agent.execute_tool(name, args)
                result = tool_result.content
                agent.record(
                    {
                        "role": "tool",
                        "name": name,
                        "args": args,
                        "content": result,
                        "created_at": now(),
                        "tool_status": str((tool_result.metadata or {}).get("tool_status", "")),
                    }
                )
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "tool_executed",
                    {
                        "name": name,
                        "args": args,
                        "result": clip(result, 500),
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                        **dict(tool_result.metadata or {}),
                    },
                )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="tool_executed")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "tool_executed",
                    },
                )
                continue

            if kind == "retry":
                agent.record({"role": "assistant", "content": payload, "created_at": now()})
                agent.run_store.write_task_state(task_state)
                continue

            final = (payload or raw).strip()
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            task_state.finish_success(final)
            agent.promote_durable_memory(user_message, final)
            checkpoint = agent.create_checkpoint(task_state, user_message, trigger="run_finished")
            agent.run_store.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "checkpoint_created",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "trigger": "run_finished",
                },
            )
            agent.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": final,
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
            return final

        if attempts >= max_attempts and tool_steps < agent.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.promote_durable_memory(user_message, final)
        agent.run_store.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger=task_state.stop_reason or "run_stopped")
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": task_state.stop_reason or "run_stopped",
            },
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        return final
