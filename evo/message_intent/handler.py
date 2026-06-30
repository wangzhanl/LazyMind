from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from pydantic import TypeAdapter, ValidationError

from evo.artifact_flow.commands import (
    ApplyArtifactMutation, CancelFlow, ContinueFlow, PauseFlow, ResumeFlow, RetryFlow,
)
from evo.artifact_runtime.evo.actions import (
    EditArtifact, InvalidateFromStep, ReadCaseArtifact, ReadStepRoot, RerunCaseStage, RerunStep,
)
from evo.artifact_runtime.kernel import ArtifactKey, ArtifactRef
from evo.service.runtime_port import RuntimePort

from .config_guard import ConfigValidationError, validate_config_patch
from .planner import StructuredPlanError, plan_next_turn
from .schemas import (
    MessageContentRef, MessageRequest, MessageTurnResult, PendingApproval, PendingInput, PlannedAction,
)
from .storage import MessageAuditStore, MessageBlobStore, MessageInProgressError, json_bytes, new_turn_id

ACTION_ADAPTER = TypeAdapter(PlannedAction)
APPROVAL_TTL_SECONDS = 86400.0


class MessageTurnHandler:
    def __init__(self, root: Path, runtime: RuntimePort,
                 flow_runner: Callable[[str, Mapping[str, Any], object], object]) -> None:
        self.runtime = runtime
        self.flow_runner = flow_runner
        self.audit = MessageAuditStore(root)
        self.blobs = MessageBlobStore(root)
        self.lock_root = root / 'message-store' / 'locks'
        self.lock_root.mkdir(parents=True, exist_ok=True)

    def handle(self, thread_id: str, request: MessageRequest) -> MessageTurnResult:
        config = self.runtime.run_config(thread_id)
        if config is None:
            raise ValueError(f'thread not found: {thread_id}')
        message_id = request.message_id or f'msg_{uuid.uuid4().hex[:16]}'
        turn_id = new_turn_id()
        lock = self._lock(thread_id)
        try:
            with lock.acquire(timeout=0):
                replay = self.audit.begin_turn(thread_id, turn_id, message_id, self._hash(request.model_dump()))
                if replay and replay.result_ref:
                    return MessageTurnResult.model_validate_json(self.blobs.load(replay.result_ref, thread_id))
                if replay and replay.resume_ref:
                    try:
                        compiled = json.loads(self.blobs.load(replay.resume_ref, thread_id))
                        self._validate_compiled(compiled, config)
                    except (ValueError, ValidationError) as exc:
                        return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                            f'历史已编译动作不可恢复，请重新发起: {exc}',
                                            projection={'pending_input_ref': None, 'pending_approval_ref': None})
                    return self._dispatch(thread_id, turn_id, message_id, compiled, config,
                                          projection={'pending_input_ref': None, 'pending_approval_ref': None})
                if replay:
                    return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                        '该 message_id 已处理，但结果不可回放')
                for attachment in request.attachments:
                    self.blobs.load(attachment, thread_id)
                msg_ref = self._blob(thread_id, turn_id, 'message_received', request.model_dump())
                self.audit.append_event(thread_id, turn_id, message_id, 'message_received', msg_ref, request.text)
                context, base_obs, projection = self._context(thread_id, turn_id, message_id, msg_ref, request, config)
                if (context.get('projection') or {}).get('projection_load_errors', {}).get('pending_approval'):
                    return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                        '待审批操作不可恢复，请重新发起。',
                                        projection={**projection, 'pending_approval_ref': None})
                try:
                    plan = plan_next_turn(context, config.get('llm_config') if isinstance(config, Mapping) else {})
                except StructuredPlanError as exc:
                    return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                        f'无法解析为结构化意图: {exc}')
                self._record_plan(thread_id, turn_id, message_id, plan)
                action = plan.next_action
                if action is None:
                    return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                        '模型没有给出可执行的结构化动作')
                agenda_id = self._agenda_id(plan.active_agenda, message_id)
                projection.update(self._agenda_projection(thread_id, turn_id, plan))
                if self.audit.projection(thread_id).get('pending_approval_ref'):
                    if action.kind in {'clarify', 'final'}:
                        return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                            '仍有待确认操作；请明确确认、取消或修正。', projection=projection)
                    if action.kind == 'approval':
                        return self._approval(thread_id, turn_id, message_id, action, base_obs, projection)
                    if plan.user_message_effect in {'amend', 'replace'}:
                        projection['pending_approval_ref'] = None
                    else:
                        return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                            '仍有待确认操作；请明确确认、取消或修正。', projection=projection)
                elif action.kind in {'clarify', 'final'}:
                    return self._finish_clarify_or_final(thread_id, turn_id, message_id, action, plan, projection)
                elif action.kind == 'approval':
                    return self._approval(thread_id, turn_id, message_id, action, base_obs, projection)
                try:
                    compiled, needs_approval, preview = self._compile(
                        thread_id, message_id, action, config, agenda_id,
                    )
                except ConfigValidationError as exc:
                    if action.kind != 'config_patch':
                        return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                            self._issues_text(exc.issues))
                    reflected = {**context, 'config_validation_issues': [item.model_dump() for item in exc.issues],
                                 'failed_action': self._safe(action)}
                    try:
                        plan = plan_next_turn(
                            reflected,
                            config.get('llm_config') if isinstance(config, Mapping) else {},
                        )
                    except StructuredPlanError as reflect_exc:
                        return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                            f'配置校验失败且反思修正失败: {reflect_exc}')
                    self._record_plan(thread_id, turn_id, message_id, plan)
                    action = plan.next_action
                    agenda_id = self._agenda_id(plan.active_agenda, message_id)
                    projection.update(self._agenda_projection(thread_id, turn_id, plan))
                    if action is None or action.kind == 'clarify':
                        text = getattr(action, 'message', '') if action is not None else plan.response_hint
                        return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                            text or self._issues_text(exc.issues), projection=projection)
                    if action.kind != 'config_patch':
                        return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                            '配置修正只能返回 corrected config_patch 或 clarification',
                                            projection=projection)
                    try:
                        compiled, needs_approval, preview = self._compile(
                            thread_id, message_id, action, config, agenda_id,
                        )
                    except (ConfigValidationError, ValueError) as retry_exc:
                        text = (
                            self._issues_text(retry_exc.issues)
                            if isinstance(retry_exc, ConfigValidationError)
                            else f'结构化意图参数无效: {retry_exc}'
                        )
                        return self._finish(thread_id, turn_id, message_id, 'needs_input', text, projection=projection)
                except ValueError as exc:
                    return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                        f'结构化意图参数无效: {exc}')
                if needs_approval:
                    return self._record_pending_approval(
                        thread_id, turn_id, message_id, action, compiled, preview, base_obs, agenda_id, projection,
                    )
                return self._dispatch(thread_id, turn_id, message_id, compiled, config,
                                      projection={**projection, 'pending_input_ref': None})
        except Timeout as exc:
            self.audit.abort_turn(thread_id, turn_id)
            raise MessageInProgressError('thread already has an active message turn') from exc
        except Exception:
            self.audit.abort_turn(thread_id, turn_id)
            raise

    def _record_plan(self, thread_id: str, turn_id: str, message_id: str, plan) -> None:
        ref = self._blob(thread_id, turn_id, 'turn_plan', plan.model_dump())
        self.audit.append_event(thread_id, turn_id, message_id, 'turn_plan', ref, plan.response_hint)

    def _context(
        self, thread_id: str, turn_id: str, message_id: str, msg_ref: MessageContentRef,
        request: MessageRequest, config: Mapping[str, Any],
    ) -> tuple[dict[str, Any], MessageContentRef, dict[str, Any]]:
        num_case = int(config.get('num_case') or (config.get('inputs') or {}).get('num_case') or 0)
        snapshot = self.runtime.query(num_case).snapshot(thread_id)
        flow_snapshot = {
            'status': snapshot.status,
            'pending_checkpoint': self._safe(snapshot.pending_checkpoint),
            'progress': self._safe(snapshot.progress),
        }
        obs = self._blob(thread_id, turn_id, 'base_observation', flow_snapshot)
        projection = self.audit.projection(thread_id)
        return {
            'thread_id': thread_id,
            'run_id': thread_id,
            'turn_id': turn_id,
            'message_id': message_id,
            'user_message_ref': msg_ref.model_dump(),
            'user_text': request.text,
            'attachments': [item.model_dump() for item in request.attachments],
            'client_context': request.client_context,
            'projection': self._projection_context(thread_id, projection),
            'base_observation_ref': obs.model_dump(),
            'flow_snapshot': flow_snapshot,
            'steps': self.runtime.spec(num_case).steps,
        }, obs, {'last_observation_ref': obs.model_dump(), 'last_observation_hash': obs.sha256}

    def _compile(self, thread_id: str, message_id: str, action: PlannedAction,
                 config: Mapping[str, Any], agenda_id: str) -> tuple[dict[str, Any], bool, dict[str, Any]]:
        num_case = int(config.get('num_case') or (config.get('inputs') or {}).get('num_case') or 0)
        spec = self.runtime.spec(num_case)
        command_id = self._command_id(thread_id, message_id, agenda_id, action)
        preview = {'action_kind': action.kind, 'agenda_item_id': agenda_id}
        if action.kind == 'query':
            if action.query == 'progress_snapshot':
                return {'type': 'query', 'query': 'progress_snapshot', 'agenda_item_id': agenda_id}, False, preview
            if action.query == 'read_step_root':
                spec.read_step_root(action.step)
                return {'type': 'query', 'query': 'read_step_root', 'step': action.step,
                        'agenda_item_id': agenda_id}, False, preview
            if action.query == 'read_case_artifact':
                spec.read_case_artifact(action.case_id, action.case_kind)
                return {'type': 'query', 'query': 'read_case_artifact', 'case_id': action.case_id,
                        'case_kind': action.case_kind, 'agenda_item_id': agenda_id}, False, preview
            raise ValueError(f'unsupported query: {action.query}')
        if action.kind == 'flow':
            if action.command not in {'continue', 'pause', 'resume', 'cancel', 'retry'}:
                raise ValueError(f'unsupported flow command: {action.command}')
            if action.until_step:
                spec._require_step(action.until_step)
            preview.update({'command': action.command, 'until_step': action.until_step})
            compiled = {'type': 'flow', 'command': action.command, 'until_step': action.until_step,
                        'command_id': command_id, 'agenda_item_id': agenda_id}
            return compiled, action.command in {'cancel'} or bool(action.until_step), preview
        if action.kind == 'mutation':
            compiled = self._mutation(spec, action, command_id)
            return {**compiled, 'agenda_item_id': agenda_id}, True, {**preview, **compiled}
        if action.kind == 'config_patch':
            compiled, patch_preview = self._config_patch(thread_id, action, command_id)
            return {**compiled, 'agenda_item_id': agenda_id}, True, {**preview, **patch_preview}
        raise ValueError(f'unsupported action kind: {action.kind}')

    def _mutation(self, spec, action: PlannedAction, command_id: str) -> dict[str, Any]:
        if action.mutation == 'rerun_case_stage':
            spec.rerun_case_stage(action.case_id, action.stage)
        elif action.mutation == 'rerun_step':
            spec.rerun_step(action.step)
        elif action.mutation == 'invalidate_from_step':
            spec.jump_to_step(action.step)
        elif action.mutation == 'edit_artifact':
            artifact_ref = self._artifact_ref(action.artifact_ref)
            spec.edit_target(artifact_ref, action.pointer)
        else:
            raise ValueError(f'unsupported mutation: {action.mutation}')
        return {'type': 'mutation', **action.model_dump(), 'command_id': command_id}

    def _config_patch(self, thread_id: str, action: PlannedAction,
                      command_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        artifact = self.runtime.config_artifact(thread_id, str(action.target))
        if artifact is None:
            raise ValueError(f'config artifact is not available: {action.target}')
        ref, current = artifact
        check = validate_config_patch(thread_id, action, ref, current)
        payload = {'kind': 'mutation', 'mutation': 'edit_artifact',
                   'artifact_ref': [check.ref.key.artifact_id, check.ref.key.partition, check.ref.version],
                   'pointer': check.pointer, 'value': check.value,
                   'config_target': action.target, 'command_id': command_id}
        return {'type': 'mutation', **payload}, check.preview

    def _finish_clarify_or_final(self, thread_id: str, turn_id: str, message_id: str,
                                 action: PlannedAction, plan, projection: dict[str, Any]) -> MessageTurnResult:
        if action.kind == 'clarify':
            pending = PendingInput(prompt=action.message or plan.response_hint)
            ref = self._blob(thread_id, turn_id, 'pending_input', pending.model_dump())
            return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                pending.prompt, pending_input_ref=ref,
                                projection={**projection, 'pending_input_ref': ref.model_dump()})
        return self._finish(thread_id, turn_id, message_id, 'final',
                            action.message or plan.response_hint,
                            projection={**projection, 'pending_input_ref': None})

    def _approval(self, thread_id: str, turn_id: str, message_id: str,
                  action: PlannedAction, base_obs: MessageContentRef,
                  projection: dict[str, Any]) -> MessageTurnResult:
        pending_ref = self.audit.projection(thread_id).get('pending_approval_ref') or {}
        if action.decision == 'reject':
            return self._finish(thread_id, turn_id, message_id, 'rejected', '已取消待审批操作',
                                projection={**projection, 'pending_approval_ref': None})
        if action.decision in {'amend', 'replace'}:
            return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                action.message or '请提供修正后的操作。',
                                projection={**projection, 'pending_approval_ref': None})
        if action.decision == 'unclear':
            return self._finish(thread_id, turn_id, message_id, 'needs_input', action.message or '请明确确认或取消。')
        if not pending_ref:
            return self._finish(thread_id, turn_id, message_id, 'needs_input', '没有可确认的待审批操作')
        try:
            pending = PendingApproval.model_validate(self._load_ref(thread_id, pending_ref))
            intent = ACTION_ADAPTER.validate_python(self._load_ref(thread_id, pending.intent_ref.model_dump()))
            original = self._load_ref(thread_id, pending.compiled_ref.model_dump())
            if not isinstance(original, Mapping):
                raise ValueError('pending compiled action must be an object')
        except (ValueError, ValidationError) as exc:
            return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                f'待审批操作不可恢复，请重新发起: {exc}',
                                projection={**projection, 'pending_approval_ref': None})
        if pending.expires_at < time.time():
            return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                '待审批操作已过期，请重新发起。',
                                projection={**projection, 'pending_approval_ref': None})
        if pending.base_observation_hash != base_obs.sha256:
            return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                '运行状态已变化，请重新确认操作。',
                                projection={**projection, 'pending_approval_ref': None})
        config = self.runtime.run_config(thread_id) or {}
        try:
            compiled, _, _ = self._compile(thread_id, pending.origin_message_id, intent, config,
                                           str(original.get('agenda_item_id') or ''))
        except (ConfigValidationError, ValueError) as exc:
            text = self._issues_text(exc.issues) if isinstance(exc, ConfigValidationError) else str(exc)
            return self._finish(thread_id, turn_id, message_id, 'needs_input', text,
                                projection={**projection, 'pending_approval_ref': None})
        if self._hash(compiled) != pending.compiled_hash:
            return self._finish(thread_id, turn_id, message_id, 'needs_input',
                                '待审批操作已变化，请重新确认。',
                                projection={**projection, 'pending_approval_ref': None})
        return self._dispatch(thread_id, turn_id, message_id, compiled, config,
                              projection={**projection, 'pending_approval_ref': None})

    def _record_pending_approval(self, thread_id: str, turn_id: str, message_id: str,
                                 action: PlannedAction, compiled: Mapping[str, Any],
                                 preview: Mapping[str, Any],
                                 base_obs: MessageContentRef, agenda_id: str,
                                 projection: dict[str, Any]) -> MessageTurnResult:
        action_hash = self._hash(action.model_dump())
        intent_ref = self._blob(thread_id, turn_id, 'pending_intent', action.model_dump())
        compiled_ref = self._blob(thread_id, turn_id, 'pending_compiled_action', compiled)
        preview_ref = self._blob(thread_id, turn_id, 'pending_preview', preview)
        pending = PendingApproval(
            approval_token=f'appr_{action_hash[:24]}',
            expires_at=time.time() + APPROVAL_TTL_SECONDS,
            origin_message_id=message_id,
            action_hash=action_hash,
            intent_ref=intent_ref,
            compiled_ref=compiled_ref,
            compiled_hash=self._hash(compiled),
            preview_ref=preview_ref,
            base_observation_hash=base_obs.sha256,
        )
        pending_ref = self._blob(thread_id, turn_id, 'pending_approval', pending.model_dump())
        self.audit.append_event(thread_id, turn_id, message_id, 'pending_approval_recorded',
                                pending_ref, agenda_id)
        return self._finish(thread_id, turn_id, message_id, 'needs_approval',
                            '需要确认后执行该操作', pending_approval_ref=pending_ref,
                            projection={**projection, 'pending_approval_ref': pending_ref.model_dump(),
                                        'pending_input_ref': None})

    def _dispatch(self, thread_id: str, turn_id: str, message_id: str, compiled: Mapping[str, Any],
                  config: Mapping[str, Any], projection: dict[str, Any] | None = None) -> MessageTurnResult:
        num_case = int(config.get('num_case') or (config.get('inputs') or {}).get('num_case') or 0)
        compiled_ref = self._blob(thread_id, turn_id, 'compiled_action', compiled)
        self.audit.append_event(thread_id, turn_id, message_id, 'compiled_action',
                                compiled_ref, str(compiled.get('type') or ''))
        if compiled['type'] == 'query':
            result = self._read(thread_id, num_case, compiled)
            turn_decision = event_kind = 'query_answered'
            text = '已读取当前信息，详细结果已写入 observation。'
        else:
            command = self._command(compiled)
            result = self.flow_runner(thread_id, config, command)
            status = str(getattr(result, 'command_status', 'ok'))
            event_kind = 'action_executed' if status == 'ok' else 'action_failed'
            turn_decision = 'action_executed' if status == 'ok' else 'needs_input'
            error = str(getattr(result, 'error', '') or '')
            text = '已执行该操作。' if status == 'ok' else f'操作未完成，状态为 {status}: {error}'.rstrip(': ')
        ref = self._blob(thread_id, turn_id, 'action_receipt', self._safe(result))
        self.audit.record_receipt(thread_id, message_id, self._hash(compiled), str(compiled.get('command_id') or ''),
                                  event_kind, ref, str(compiled.get('agenda_item_id') or ''))
        self.audit.append_event(thread_id, turn_id, message_id, event_kind, ref, event_kind)
        snapshot = self.runtime.query(num_case).snapshot(thread_id)
        obs = self._blob(thread_id, turn_id, 'observation', self._safe(snapshot))
        projection = {**(projection or {}), 'last_observation_ref': obs.model_dump(),
                      'last_observation_hash': obs.sha256}
        return self._finish(thread_id, turn_id, message_id, turn_decision, text, observation_ref=obs,
                            action_receipt_ref=ref, projection=projection)

    def _read(self, thread_id: str, num_case: int, compiled: Mapping[str, Any]) -> Any:
        query = compiled['query']
        if query == 'progress_snapshot':
            return self.runtime.query(num_case).snapshot(thread_id)
        if query == 'read_step_root':
            return self.runtime.query(num_case).read(thread_id, ReadStepRoot(str(compiled['step'])))
        return self.runtime.query(num_case).read(
            thread_id, ReadCaseArtifact(str(compiled['case_id']), str(compiled['case_kind'])),
        )

    def _validate_compiled(self, value: Any, config: Mapping[str, Any]) -> None:
        if not isinstance(value, Mapping):
            raise ValueError('compiled action must be an object')
        num_case = int(config.get('num_case') or (config.get('inputs') or {}).get('num_case') or 0)
        spec = self.runtime.spec(num_case)
        action_type = value.get('type')
        if action_type == 'query':
            query = value.get('query')
            if query not in {'progress_snapshot', 'read_step_root', 'read_case_artifact'}:
                raise ValueError('compiled query is invalid')
            if query == 'read_step_root':
                spec.read_step_root(str(value.get('step') or ''))
            if query == 'read_case_artifact':
                spec.read_case_artifact(str(value.get('case_id') or ''), str(value.get('case_kind') or ''))
            return
        if action_type == 'flow':
            if value.get('command') not in {'continue', 'pause', 'resume', 'cancel', 'retry'}:
                raise ValueError('compiled flow command is invalid')
            if not isinstance(value.get('command_id'), str) or not str(value.get('command_id')).strip():
                raise ValueError('compiled flow command_id is required')
            if value.get('until_step'):
                spec._require_step(str(value.get('until_step') or ''))
            return
        if action_type == 'mutation':
            mutation = value.get('mutation')
            if mutation not in {'edit_artifact', 'rerun_case_stage', 'rerun_step', 'invalidate_from_step'}:
                raise ValueError('compiled mutation is invalid')
            if not isinstance(value.get('command_id'), str) or not str(value.get('command_id')).strip():
                raise ValueError('compiled mutation command_id is required')
            if mutation == 'rerun_case_stage':
                spec.rerun_case_stage(str(value.get('case_id') or ''), str(value.get('stage') or ''))
            if mutation == 'rerun_step':
                spec.rerun_step(str(value.get('step') or ''))
            if mutation == 'invalidate_from_step':
                spec.jump_to_step(str(value.get('step') or ''))
            if mutation == 'edit_artifact':
                spec.edit_target(self._artifact_ref(value.get('artifact_ref')), str(value.get('pointer') or ''))
            return
        raise ValueError('compiled action type is invalid')

    def _command(self, compiled: Mapping[str, Any]):
        command_id = str(compiled.get('command_id') or '')
        if compiled['type'] == 'flow':
            return {
                'continue': ContinueFlow(command_id, str(compiled.get('until_step') or '')),
                'pause': PauseFlow(command_id),
                'resume': ResumeFlow(command_id),
                'cancel': CancelFlow(command_id),
                'retry': RetryFlow(command_id),
            }[str(compiled['command'])]
        mutation = str(compiled['mutation'])
        if mutation == 'rerun_case_stage':
            action = RerunCaseStage(str(compiled['case_id']), str(compiled['stage']), command_id)
        elif mutation == 'rerun_step':
            action = RerunStep(str(compiled['step']), command_id)
        elif mutation == 'invalidate_from_step':
            action = InvalidateFromStep(str(compiled['step']), command_id)
        else:
            action = EditArtifact(self._artifact_ref(compiled['artifact_ref']), str(compiled['pointer']),
                                  compiled.get('value'), command_id)
        return ApplyArtifactMutation(command_id, action)

    def _agenda_projection(self, thread_id: str, turn_id: str, plan) -> dict[str, Any]:
        value = [item.model_dump() for item in plan.active_agenda]
        ref = self._blob(thread_id, turn_id, 'active_agenda', value) if value else None
        return {'active_agenda_ref': None if ref is None else ref.model_dump()}

    def _projection_context(self, thread_id: str, projection: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(projection)
        errors = {}
        for key in ('active_agenda_ref', 'pending_input_ref', 'pending_approval_ref'):
            ref = projection.get(key)
            name = key[:-4]
            try:
                data[name] = self._load_ref(thread_id, ref) if ref else {}
            except (ValueError, ValidationError) as exc:
                data[name] = {'load_error': str(exc)}
                errors[name] = str(exc)
        if errors:
            data['projection_load_errors'] = errors
        return data

    def _load_ref(self, thread_id: str, ref: Any) -> Any:
        content_ref = MessageContentRef.model_validate(ref)
        return json.loads(self.blobs.load(content_ref, thread_id))

    def _finish(self, thread_id: str, turn_id: str, message_id: str, decision: str, text: str,
                projection: dict[str, Any] | None = None, **refs: Any) -> MessageTurnResult:
        text_ref = self._blob(thread_id, turn_id, 'assistant_text', {'text': text})
        self.audit.append_event(thread_id, turn_id, message_id, 'assistant_response', text_ref, text)
        result = MessageTurnResult(thread_id=thread_id, turn_id=turn_id, message_id=message_id,
                                   turn_decision=decision, assistant_text=text,
                                   assistant_text_ref=text_ref, **refs)
        result_ref = self._blob(thread_id, turn_id, 'turn_result', result.model_dump())
        self.audit.finish_turn(thread_id, turn_id, result_ref, projection)
        return result

    def _blob(self, thread_id: str, turn_id: str, kind: str, value: object) -> MessageContentRef:
        return self.blobs.append(thread_id, turn_id, kind, json_bytes(self._safe(value)))

    def _lock(self, thread_id: str) -> FileLock:
        return FileLock(str(self.lock_root / f'{self._hash(thread_id)[:32]}.lock'))

    @staticmethod
    def _agenda_id(agenda: list[Any], message_id: str) -> str:
        if agenda and getattr(agenda[0], 'agenda_item_id', ''):
            return str(agenda[0].agenda_item_id)
        return f'agenda:{message_id}'

    @classmethod
    def _command_id(cls, thread_id: str, message_id: str, agenda_id: str, action: PlannedAction) -> str:
        payload = {'agenda_item_id': agenda_id, 'action': action.model_dump()}
        return f'msgi:{thread_id}:{message_id}:{cls._hash(payload)[:24]}'

    @staticmethod
    def _artifact_ref(value: Any) -> ArtifactRef:
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError('artifact_ref must be [artifact_id, partition, version]')
        try:
            version = int(value[2])
        except (TypeError, ValueError) as exc:
            raise ValueError('artifact_ref version must be int') from exc
        return ArtifactRef(ArtifactKey(str(value[0]), str(value[1])), version)

    @staticmethod
    def _issues_text(issues: list[Any]) -> str:
        return '；'.join(str(getattr(issue, 'message', issue)) for issue in issues) or '配置参数无效'

    @classmethod
    def _hash(cls, value: object) -> str:
        return hashlib.sha256(json_bytes(cls._safe(value))).hexdigest()

    @classmethod
    def _safe(cls, value: object) -> object:
        if hasattr(value, 'model_dump'):
            return value.model_dump()
        if isinstance(value, Mapping):
            return {str(key): cls._safe(item) for key, item in value.items()}
        if hasattr(value, '__dict__'):
            return {key: cls._safe(item) for key, item in vars(value).items()}
        if isinstance(value, (list, tuple)):
            return [cls._safe(item) for item in value]
        return value
