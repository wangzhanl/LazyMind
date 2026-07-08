from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from unidiff import PatchSet

from evo.operations.public_contracts import RepairPatch, algo_id, dump_contract

from .candidate import validate_candidate_patch
from .code_index import build_code_index
from .fallback import generate_fallback_patch
from .localize import localize_repair
from .opencode import run_opencode_streaming
from .report import read_worker_report
from .trace import safe_emit, trace_cursor
from .validation import pre_validate
from .workspace import (
    algorithm_source_root,
    git,
    prepare_workspace,
    reset_workspace,
    source_fingerprint,
    workspace_diff,
    workspace_fingerprint,
    workspace_path,
)

DEFAULT_SOURCE = '/app/algorithm'
PROVIDER_NAME = re.compile(r'^[a-z0-9][a-z0-9_-]*$')
PATCH_STATUSES = {
    'candidate_rejected_patch',
    'external_blocked_with_patch',
    'fallback_patch_generated',
    'pre_validation_failed_patch',
    'protocol_failed_with_patch',
    'verified',
}


def prepare_candidate_workspace(
    plan: Mapping[str, Any],
    repair_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if plan.get('status') != 'planned':
        return {'status': 'skipped', 'repair_plan_ref': _plan_ref(plan), 'workspace_kind': 'managed_worktree'}
    policy = _runtime_policy(plan, repair_policy)
    source = algorithm_source_root(policy.get('candidate_source_dir') or os.getenv('LAZYMIND_EVO_CHAT_SOURCE')
                                   or DEFAULT_SOURCE)
    workspace = workspace_path(policy, plan)
    objective_hash = hashlib.sha1(json.dumps(plan.get('objective') or {}, sort_keys=True).encode()).hexdigest()[:12]
    prepare_workspace(source, workspace, objective_hash)
    source_hash = source_fingerprint(source)['source_hash']
    return {
        'status': 'ready',
        'workspace_kind': 'managed_worktree',
        'workspace_ref': str(workspace),
        'source_dir': str(source),
        'source_hash': source_hash,
        'objective_hash': objective_hash,
        'git_head': git(workspace, 'rev-parse', '--verify', 'HEAD'),
        'repair_plan_ref': _plan_ref(plan),
    }


def run_repair_loop(workspace: Mapping[str, Any], cases: tuple[Mapping[str, Any], ...],
                    baseline_judges: tuple[Mapping[str, Any], ...], eval_policy: Mapping[str, Any],
                    candidate_config: Mapping[str, Any], repair_policy: Mapping[str, Any], ctx: Any,
                    plan: Mapping[str, Any] | None = None,
                    trace: Any | None = None) -> dict[str, Any]:
    plan = plan if isinstance(plan, Mapping) else {}
    baseline_algo_id = next((text for judge in baseline_judges for text in (algo_id(judge),) if text), '')
    ready = _ready_workspace(workspace, plan, repair_policy)
    if ready.get('status') != 'ready':
        forced = _force_protocol_patch(plan, repair_policy, trace, _text(ready.get('reason')))
        attempts = [forced] if forced.get('diff') else []
        status = _text(forced.get('status')) if attempts else 'protocol_failed'
        if attempts and status not in PATCH_STATUSES:
            status = 'protocol_failed_with_patch'
        return _result(status, plan, workspace, attempts, forced, ready['reason'], baseline_algo_id,
                       trace_cursor(trace))
    root = Path(str(workspace['workspace_ref'])).resolve()
    case_map = {_text(case.get('id')): case for case in cases
                if isinstance(case, Mapping) and _text(case.get('id'))}
    baseline_map = {_text(judge.get('case_id')): judge for judge in baseline_judges
                    if isinstance(judge, Mapping) and _text(judge.get('case_id'))}
    policy = _runtime_policy(plan, repair_policy)
    index = build_code_index(root)
    localization = localize_repair(index, plan)
    opencode_config = _opencode_config_from_policy(policy)
    attempts, session_id = [], ''
    budget = _int(policy.get('repair_attempt_budget'), 3, 1, 20)
    best_patch: Mapping[str, Any] = {}

    for attempt_no in range(1, budget + 1):
        safe_emit(trace, 'repair.attempt_started', status='started', attempt=attempt_no,
                  payload={'budget': budget, 'localization_status': localization.get('status')})
        reset_workspace(root)
        artifact_dir = root / '.evo_repair_logs' / 'opencode' / f'attempt_{attempt_no}'
        run = None
        if opencode_config:
            task = _task_card(plan, workspace, localization, attempt_no, artifact_dir / 'worker_report.json')
            run = run_opencode_streaming(
                workdir=str(root),
                prompt=json.dumps(task, ensure_ascii=False, indent=2),
                artifact_dir=artifact_dir,
                session_id=session_id,
                config=opencode_config,
                timeout_s=_int(policy.get('opencode_timeout_s') or os.getenv('LAZYMIND_EVO_CODE_TIMEOUT_S'),
                               900, 30, 7200),
                trace=trace,
                attempt=attempt_no,
            )
            session_id = run.session_id or session_id
        report = read_worker_report(artifact_dir / 'worker_report.json')
        diff_info = workspace_diff(root)
        fallback = {}
        if not diff_info['diff'].strip():
            fallback = generate_fallback_patch(root, index, localization)
            diff_info = workspace_diff(root)
        pre = pre_validate(root, diff_info, plan, policy, trace, attempt_no) if diff_info['diff'].strip() else {
            'status': 'failed',
            'reason': 'fallback_failed_to_create_diff',
            'commands': [],
        }
        if _must_replace_with_fallback(pre, fallback):
            reset_workspace(root)
            fallback = generate_fallback_patch(root, index, localization)
            diff_info = workspace_diff(root)
            pre = pre_validate(root, diff_info, plan, policy, trace, attempt_no) if diff_info['diff'].strip() else {
                'status': 'failed',
                'reason': 'fallback_failed_to_create_diff',
                'commands': [],
            }
        candidate = (
            validate_candidate_patch(root, diff_info['diff'], plan, case_map, baseline_map, eval_policy,
                                     candidate_config, ctx, trace, attempt_no)
            if pre.get('status') == 'passed' and case_map and baseline_map
            else {'status': 'skipped', 'accepted': False, 'reason': _text(pre.get('reason')) or 'pre_validation_failed'}
        )
        status = _attempt_status(pre, candidate, run, fallback)
        attempt = {
            'attempt': attempt_no,
            'status': status,
            'opencode': {
                'returncode': getattr(run, 'returncode', None),
                'last_error': getattr(run, 'last_error', None),
                'configured': bool(opencode_config),
            },
            'worker_report': report,
            'localization': localization,
            'fallback': fallback,
            'pre_validation': pre,
            'candidate_validation': candidate,
            'workspace_ref': str(root),
            'files_changed': diff_info['files'],
            'diff': diff_info['diff'],
        }
        attempts.append(attempt)
        if diff_info['diff'].strip() and not best_patch:
            best_patch = attempt
        if status == 'verified':
            safe_emit(trace, 'repair.loop_completed', status='completed', terminal=True,
                      payload={'status': 'validated', 'attempt_count': len(attempts)})
            return _result('validated', plan, workspace, attempts, attempt, 'validated repair patch',
                           baseline_algo_id, trace_cursor(trace))
        if diff_info['diff'].strip():
            best_patch = _prefer_patch(best_patch, attempt)
    if not best_patch:
        forced = _force_patch(root, plan, policy, trace, 'final_empty_patch')
        if forced.get('diff'):
            attempts.append(forced)
            best_patch = forced
    winner = best_patch or {}
    status = _text(winner.get('status')) or 'protocol_failed'
    safe_emit(trace, 'repair.loop_completed', status='failed', terminal=True,
              payload={'status': status, 'attempt_count': len(attempts)})
    return _result(status, plan, workspace, attempts, winner, f'repair ended with {status}',
                   baseline_algo_id, trace_cursor(trace))


def build_verified_patch(run_id: str, loop: Mapping[str, Any]) -> dict[str, Any]:
    raw_diff = loop.get('winning_patch_diff')
    diff = raw_diff if isinstance(raw_diff, str) else str(raw_diff or '')
    status = 'verified' if loop.get('status') == 'validated' else _text(loop.get('status'))
    if status not in PATCH_STATUSES:
        status = 'protocol_failed_with_patch'
    diff_by_file = _diff_by_file(diff)
    if not diff_by_file:
        raise ValueError('repair patch contract requires a non-empty diff')
    return dump_contract(RepairPatch, {
        'run_id': run_id,
        'algo_id': _text(loop.get('algo_id')),
        'candidate_algo_id': _text(loop.get('candidate_algo_id')),
        'status': status,
        'workspace_ref': _text(loop.get('workspace_ref')),
        'diff': diff_by_file,
    })


def _must_replace_with_fallback(pre: Mapping[str, Any], fallback: Mapping[str, Any]) -> bool:
    if fallback.get('status') == 'patched':
        return False
    return _text(pre.get('reason')) in {
        'ast_unchanged',
        'diff_scope_violation',
        'forbidden_trivial_construct',
        'no_domain_python_change',
        'python_ast_parse_failed',
        'trivial_patch_only',
    }


def _force_protocol_patch(
    plan: Mapping[str, Any],
    repair_policy: Mapping[str, Any],
    trace: Any | None,
    reason: str,
) -> Mapping[str, Any]:
    policy = _runtime_policy(plan, repair_policy)
    try:
        source = algorithm_source_root(policy.get('candidate_source_dir') or os.getenv('LAZYMIND_EVO_CHAT_SOURCE')
                                       or DEFAULT_SOURCE)
        root = workspace_path(policy, plan)
        objective_hash = hashlib.sha1(json.dumps(plan.get('objective') or {}, sort_keys=True).encode()).hexdigest()[:12]
        prepare_workspace(source, root, objective_hash)
    except (OSError, RuntimeError, ValueError):
        return {}
    return _force_patch(root.resolve(), plan, policy, trace, reason)


def _force_patch(
    root: Path,
    plan: Mapping[str, Any],
    policy: Mapping[str, Any],
    trace: Any | None,
    reason: str,
) -> Mapping[str, Any]:
    if not _patchable_workspace(root):
        return {}
    try:
        reset_workspace(root)
        index = build_code_index(root)
        localization = localize_repair(index, plan)
        fallback = generate_fallback_patch(root, index, localization)
        diff_info = workspace_diff(root)
    except (OSError, RuntimeError, ValueError):
        return {}
    if not diff_info['diff'].strip():
        return {}
    pre = pre_validate(root, diff_info, plan, policy, trace, 0)
    return {
        'attempt': 0,
        'status': 'protocol_failed_with_patch' if pre.get('status') != 'passed' else 'fallback_patch_generated',
        'opencode': {'returncode': None, 'last_error': reason, 'configured': False},
        'worker_report': {},
        'localization': localization,
        'fallback': fallback,
        'pre_validation': pre,
        'candidate_validation': {'status': 'skipped', 'accepted': False, 'reason': reason},
        'workspace_ref': str(root),
        'files_changed': diff_info['files'],
        'diff': diff_info['diff'],
    }


def _patchable_workspace(root: Path) -> bool:
    return (
        root.exists()
        and (root / '.git').exists()
        and any((root / base).exists() for base in ('lazymind/chat', 'lazymind/parsing'))
    )


def _ready_workspace(workspace: Mapping[str, Any], plan: Mapping[str, Any],
                     repair_policy: Mapping[str, Any]) -> dict[str, str]:
    if workspace.get('status') != 'ready' or plan.get('status') != 'planned':
        return {'status': 'failed', 'reason': 'repair plan is not runnable'}
    policy = _runtime_policy(plan, repair_policy)
    objective_hash = hashlib.sha1(json.dumps(plan.get('objective') or {}, sort_keys=True).encode()).hexdigest()[:12]
    try:
        root = Path(_text(workspace.get('workspace_ref'))).resolve()
        expected_root = workspace_path(policy, plan).resolve()
        fingerprint = workspace_fingerprint(root)
        workspace_head = git(root, 'rev-parse', '--verify', 'HEAD')
    except (OSError, RuntimeError, ValueError):
        return {'status': 'failed', 'reason': 'candidate workspace artifact failed integrity check'}
    if (
        root != expected_root
        or fingerprint.get('source_hash') != workspace.get('source_hash')
        or fingerprint.get('source_dir') != workspace.get('source_dir')
        or fingerprint.get('objective_hash') != objective_hash
        or workspace_head != workspace.get('git_head')
        or not (root / 'lazymind' / 'chat').exists()
    ):
        return {'status': 'failed', 'reason': 'candidate workspace artifact failed integrity check'}
    return {'status': 'ready', 'reason': ''}


def _attempt_status(pre: Mapping[str, Any], candidate: Mapping[str, Any], run: Any, fallback: Mapping[str, Any]) -> str:
    if candidate.get('accepted'):
        return 'verified'
    if pre.get('status') != 'passed':
        return 'pre_validation_failed_patch'
    if fallback.get('status') == 'patched' and candidate.get('status') == 'skipped':
        return 'fallback_patch_generated'
    if getattr(run, 'returncode', 0) or getattr(run, 'last_error', None):
        return 'external_blocked_with_patch'
    return 'candidate_rejected_patch'


def _prefer_patch(current: Mapping[str, Any], attempt: Mapping[str, Any]) -> Mapping[str, Any]:
    rank = {
        'verified': 5,
        'candidate_rejected_patch': 4,
        'fallback_patch_generated': 3,
        'pre_validation_failed_patch': 2,
        'external_blocked_with_patch': 1,
    }
    return attempt if rank.get(_text(attempt.get('status')), 0) > rank.get(_text(current.get('status')), 0) else current


def _task_card(plan: Mapping[str, Any], workspace: Mapping[str, Any], localization: Mapping[str, Any],
               attempt: int, report_path: Path) -> dict[str, Any]:
    return {
        'mode': 'lazyrag_force_patch_repair_v2',
        'attempt': attempt,
        'objective': plan.get('objective'),
        'brief': plan.get('brief') if isinstance(plan.get('brief'), Mapping) else {},
        'workspace': {'path': workspace.get('workspace_ref'), 'source_dir': workspace.get('source_dir')},
        'localization': {
            'domain': localization.get('domain'),
            'ranked_symbols': list(localization.get('ranked_symbols') or ())[:12],
            'weak_hints': localization.get('weak_hints'),
        },
        'hard_constraints': [
            'Leave a non-empty git diff. Do not stop without editing code.',
            'Edit only under lazymind/chat or lazymind/parsing.',
            'Do not edit tests, eval, data, generated files, secrets, or vendored lazyllm.',
            'Use ranked symbols as localization evidence, not as a hard file whitelist.',
            'The host repair loop runs verification; do not claim to run commands you cannot run.',
            f'Write a JSON worker report to {report_path.as_posix()}.',
        ],
        'worker_report_schema': {
            'status': 'edited',
            'mode': 'patch',
            'files_changed': ['lazymind/chat/... or lazymind/parsing/...'],
            'confirmed_locations': [{'path': '...', 'symbol': '...', 'line_start': 1, 'line_end': 2,
                                     'evidence': '...'}],
            'touched_symbols': ['...'],
            'change_intent': 'minimal behaviorful repair',
            'risk': 'low|medium|high',
            'notes': '',
        },
    }


def _result(status: str, plan: Mapping[str, Any], workspace: Mapping[str, Any], attempts: list[Mapping[str, Any]],
            best: Mapping[str, Any], message: str, algo_id_value: str = '',
            trace_cursor: Mapping[str, Any] | None = None) -> dict[str, Any]:
    candidate = best.get('candidate_validation') if isinstance(best.get('candidate_validation'), Mapping) else {}
    service = candidate.get('service') if isinstance(candidate.get('service'), Mapping) else {}
    diff = str(best.get('diff') or '')
    if status.endswith('_with_patch') and not diff.strip():
        status = 'protocol_failed'
    workspace_ref = _text(best.get('workspace_ref')) or _text(workspace.get('workspace_ref'))
    return {
        'id': 'repair.loop_result',
        'status': status,
        'message': message,
        'algo_id': algo_id_value,
        'attempt_count': len(attempts),
        'files_changed': best.get('files_changed') or [],
        'workspace_ref': workspace_ref,
        'candidate_algo_id': _text(service.get('algorithm_id')),
        'winning_patch_diff': diff,
        'selected_group': _group_summary(plan.get('selected_group')),
        'attempts': attempts,
        'trace_cursor': dict(trace_cursor or {}),
    }


def _diff_by_file(diff: str) -> dict[str, str]:
    if not diff.strip():
        return {}
    result = {}
    for patched in PatchSet(diff.splitlines(True)):
        source = _text(patched.source_file).removeprefix('a/')
        target = _text(patched.target_file).removeprefix('b/')
        path = source if target == '/dev/null' else target or source
        if path:
            result[path] = str(patched)
    return result


def _group_summary(value: object) -> dict[str, Any]:
    group = value if isinstance(value, Mapping) else {}
    return _pick(group, (
        'group_id',
        'function_block_id',
        'issue_type',
        'failure_mode',
        'badcase_count',
        'representative_case_id',
        'confidence_score',
        'candidate_files',
        'case_ids',
        'trace_cluster_id',
        'trace_signature',
    ))


def _plan_ref(plan: Mapping[str, Any]) -> dict[str, Any]:
    objective_hash = hashlib.sha1(
        json.dumps(plan.get('objective') or {}, sort_keys=True).encode()
    ).hexdigest()[:12]
    return {
        'status': _text(plan.get('status')),
        'objective_hash': objective_hash,
        'selected_group': _group_summary(plan.get('selected_group')),
    }


def _opencode_config_from_policy(policy: Mapping[str, Any]) -> dict[str, str]:
    llm_config = policy.get('llm_config') if isinstance(policy.get('llm_config'), Mapping) else {}
    role = llm_config.get('evo_llm') if isinstance(llm_config.get('evo_llm'), Mapping) else {}
    if not role:
        return {}
    model = _text(role.get('model'))
    base_url = _text(role.get('base_url')).rstrip('/')
    provider = _text(role.get('provider') or role.get('source')).lower()
    if not PROVIDER_NAME.fullmatch(provider):
        return {}
    if provider == 'qwen' and base_url == 'https://dashscope.aliyuncs.com':
        base_url = f'{base_url}/compatible-mode/v1'
    api_key = _text(role.get('api_key'))
    skip_auth = role.get('skip_auth') is True
    if not (provider and model and base_url and (api_key or skip_auth)):
        return {}
    return {
        'model': f'{provider}/{model}',
        'provider': provider,
        'provider_model': model,
        'provider_label': provider,
        'base_url': base_url,
        'api_key': api_key,
        'skip_auth': 'true' if skip_auth else '',
    }


def _runtime_policy(plan: Mapping[str, Any], repair_policy: Mapping[str, Any] | None) -> dict[str, Any]:
    safe = plan.get('policy') if isinstance(plan.get('policy'), Mapping) else {}
    raw = repair_policy if isinstance(repair_policy, Mapping) else {}
    safe_keys = {
        'allowed_roots',
        'blocked_roots',
        'target_case_budget',
        'neighbor_case_budget',
        'goodcase_guard_budget',
        'cross_block_guard_budget',
        'evidence_case_budget',
        'seed_file_budget',
    }
    runtime_keys = {
        'candidate_source_dir',
        'candidate_workdir',
        'workspace_namespace',
        'thread_id',
        'verification_commands',
        'repair_attempt_budget',
        'opencode_timeout_s',
        'max_patch_bytes',
        'llm_config',
    }
    return {
        **{key: safe[key] for key in safe_keys if key in safe},
        **{key: raw[key] for key in runtime_keys if key in raw},
    }


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _text(value: Any) -> str:
    return str(value or '').strip()


def _pick(value: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: value[key] for key in keys if key in value}
