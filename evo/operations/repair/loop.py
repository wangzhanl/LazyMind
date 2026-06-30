from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evo.operations.abtest.materializers import (
    candidate_judge_result,
    candidate_rag_answer,
    candidate_service,
    compare_abtest,
)
from evo.operations.analysis.summary import build_analysis_from_answers
from evo.operations.eval.materializers import summarize_eval

from .opencode import run_opencode_streaming, trace_payload

DEFAULT_SOURCE = '/app/algorithm'
DEFAULT_VERIFY = ('python -m compileall -q lazymind/chat',)
PUBLIC_SERVICE_KEYS = {
    'status',
    'service_kind',
    'algorithm_id',
    'service_url',
    'router_admin_url',
    'workspace_ref',
    'code_path',
}


def prepare_candidate_workspace(
    plan: Mapping[str, Any],
    repair_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if plan.get('status') != 'planned':
        return {'status': 'skipped', 'repair_plan': dict(plan), 'workspace_kind': 'managed_worktree'}
    policy = _runtime_policy(plan, repair_policy)
    source = _algorithm_source_root(policy.get('candidate_source_dir') or os.getenv('LAZYMIND_EVO_CHAT_SOURCE')
                                    or DEFAULT_SOURCE)
    workspace = _workspace_path(policy, plan)
    objective_hash = hashlib.sha1(json.dumps(plan.get('objective') or {}, sort_keys=True).encode()).hexdigest()[:12]
    _prepare_workspace(source, workspace, objective_hash)
    source_hash = _source_fingerprint(source)['source_hash']
    return {
        'status': 'ready',
        'workspace_kind': 'managed_worktree',
        'workspace_ref': str(workspace),
        'source_dir': str(source),
        'source_hash': source_hash,
        'objective_hash': objective_hash,
        'git_head': _git(workspace, 'rev-parse', '--verify', 'HEAD'),
        'repair_plan': dict(plan),
    }


def run_repair_loop(workspace: Mapping[str, Any], cases: tuple[Mapping[str, Any], ...],
                    baseline_judges: tuple[Mapping[str, Any], ...], eval_policy: Mapping[str, Any],
                    candidate_config: Mapping[str, Any], repair_policy: Mapping[str, Any], ctx: Any
                    ) -> dict[str, Any]:
    plan = workspace.get('repair_plan') if isinstance(workspace.get('repair_plan'), Mapping) else {}
    if workspace.get('status') != 'ready' or plan.get('status') != 'planned':
        return _result('skipped', plan, workspace, [], {}, 'repair plan is not runnable')
    policy = _runtime_policy(plan, repair_policy)
    if not _text(workspace.get('workspace_ref')):
        return _result('blocked', plan, workspace, [], {}, 'missing candidate workspace_ref')
    objective_hash = hashlib.sha1(json.dumps(plan.get('objective') or {}, sort_keys=True).encode()).hexdigest()[:12]
    try:
        root = Path(_text(workspace.get('workspace_ref'))).resolve()
        expected_root = _workspace_path(policy, plan).resolve()
        fingerprint = _workspace_fingerprint(root)
        workspace_head = _git(root, 'rev-parse', '--verify', 'HEAD')
    except (OSError, RuntimeError, ValueError):
        return _result('blocked', plan, workspace, [], {}, 'candidate workspace artifact failed integrity check')
    if (
        root != expected_root
        or fingerprint.get('source_hash') != workspace.get('source_hash')
        or fingerprint.get('source_dir') != workspace.get('source_dir')
        or fingerprint.get('objective_hash') != objective_hash
        or workspace_head != workspace.get('git_head')
        or not (root / 'lazymind' / 'chat' / 'app.py').exists()
    ):
        return _result('blocked', plan, workspace, [], {}, 'candidate workspace artifact failed integrity check')
    case_map = {_text(case.get('id')): case for case in cases
                if isinstance(case, Mapping) and _text(case.get('id'))}
    baseline_map = {_text(judge.get('case_id')): judge for judge in baseline_judges
                    if isinstance(judge, Mapping) and _text(judge.get('case_id'))}
    if not case_map or not baseline_map:
        return _result('blocked', plan, workspace, [], {}, 'repair requires real cases and baseline judges')
    env = _opencode_env(policy)
    if not env:
        return _result('blocked', plan, workspace, [], {}, 'missing opencode model configuration')
    attempts, base_diff, best = [], '', {}
    rank = {'resolved': 3, 'improved': 2, 'unchanged': 1, 'regressed': 0}
    budget = _int(policy.get('repair_attempt_budget'), 30, 1, 100)
    session_id = ''
    for attempt_no in range(1, budget + 1):
        _reset_workspace(root)
        if base_diff:
            _apply_diff(root, base_diff)
        task = _task_card(plan, workspace, attempt_no, attempts)
        run = run_opencode_streaming(
            workdir=str(root),
            prompt=json.dumps(task, ensure_ascii=False, indent=2),
            artifact_dir=root / '.evo_repair_logs' / 'opencode' / f'attempt_{attempt_no}',
            session_id=session_id,
            env=env,
            timeout_s=_int(policy.get('opencode_timeout_s') or os.getenv('LAZYMIND_EVO_CODE_TIMEOUT_S'), 900, 30, 7200),
            first_response_timeout_s=_int(policy.get('opencode_first_response_timeout_s'), 300, 10, 1800),
        )
        session_id = run.session_id or session_id
        diff_info = _diff(root)
        pre = (
            _opencode_failure(run)
            if run.returncode or run.last_error
            else _pre_validate(root, diff_info, plan, policy)
        )
        candidate = _candidate_validation(root, diff_info['diff'], plan, case_map, baseline_map, eval_policy,
                                          candidate_config, ctx) if pre['status'] == 'passed' else {
            'status': 'skipped',
            'accepted': False,
            'reason': 'pre_validation_failed',
        }
        delta = (
            dict(candidate['analysis_delta'])
            if isinstance(candidate.get('analysis_delta'), Mapping)
            else {'status': 'skipped', 'target_group_status': 'not_evaluated',
                  'recommended_action': 'rollback_to_baseline'}
        )
        decision = _patch_base_decision(pre, candidate, delta, best)
        trace = trace_payload(run, 'repair.plan', attempt_no)
        attempt = {
            'attempt': attempt_no,
            'status': 'validated' if decision['action'] == 'accept_patch' else 'failed',
            'base': {'mode': 'baseline' if not base_diff else 'continued_patch'},
            'opencode_trace': trace,
            'pre_validation': pre,
            'candidate_validation': candidate,
            'candidate_analysis': candidate.get('candidate_analysis') or {},
            'analysis_delta': delta,
            'patch_base_decision': decision,
            'files_changed': diff_info['files'],
            'diff': diff_info['diff'],
            'events': _attempt_events(attempt_no, trace, pre, candidate, delta, decision),
        }
        attempts.append(attempt)
        best_delta = best.get('analysis_delta') if isinstance(best.get('analysis_delta'), Mapping) else {}
        best_metrics = best_delta.get('metric_delta') if isinstance(best_delta.get('metric_delta'), Mapping) else {}
        attempt_metrics = delta.get('metric_delta') if isinstance(delta.get('metric_delta'), Mapping) else {}
        attempt_score = (
            rank.get(delta.get('target_group_status'), 0),
            float(attempt_metrics.get('answer_correctness') or attempt_metrics.get('overall_score') or 0.0),
        )
        best_score = (
            rank.get(best_delta.get('target_group_status'), 0),
            float(best_metrics.get('answer_correctness') or best_metrics.get('overall_score') or 0.0),
        )
        retryable_best = (
            pre.get('status') == 'passed'
            and delta.get('status') == 'completed'
            and not delta.get('new_group_count')
            and (
                delta.get('target_group_status') == 'resolved'
                or (
                    delta.get('target_group_status') == 'improved'
                    and float(attempt_metrics.get('answer_correctness')
                              or attempt_metrics.get('overall_score') or 0.0) > 0.0
                )
            )
        )
        if retryable_best and (not best or attempt_score > best_score):
            best = dict(attempt)
        if decision['action'] == 'accept_patch':
            return _result('validated', plan, workspace, attempts, attempt, 'validated repair patch')
        if decision['action'] == 'blocked':
            _reset_workspace(root)
            return _result('blocked', plan, workspace, attempts, {}, decision['reason'])
        base_diff = (
            diff_info['diff']
            if decision['action'] in {'continue_current_patch', 'fork_from_best_attempt'}
            else ''
        )
        if decision['action'] == 'fork_from_best_attempt':
            base_diff = best.get('diff') or ''
    _reset_workspace(root)
    return _result('no_validated_patch', plan, workspace, attempts, best, f'attempt budget exhausted: {budget}')


def build_verified_patch(loop: Mapping[str, Any]) -> dict[str, Any]:
    attempts = [item for item in loop.get('attempts') or [] if isinstance(item, Mapping)]
    winner = next((item for item in reversed(attempts) if _validated_attempt(item)), {})
    if not winner:
        status = 'blocked' if loop.get('status') == 'blocked' else 'no_patch'
        return {
            'status': status,
            'diff': '',
            'patch': '',
            'content': 'No verified code changes were produced for this repair step.\n',
            'repair_loop': dict(loop),
            'workspace_ref': _text(loop.get('workspace_ref')),
            'files': [],
            'winning_attempt': None,
            'validation_summary': {},
        }
    diff = _text(winner.get('diff'))
    return {
        'status': 'verified',
        'diff': diff,
        'patch': diff,
        'content': diff,
        'repair_loop': dict(loop),
        'workspace_ref': _text(loop.get('workspace_ref')),
        'files': list(winner.get('files_changed') or []),
        'winning_attempt': winner.get('attempt'),
        'validation_summary': winner.get('candidate_validation') or {},
        'events': list(loop.get('events') or []),
    }


def _candidate_validation(
    root: Path,
    diff: str,
    plan: Mapping[str, Any],
    cases: Mapping[str, Mapping[str, Any]],
    baseline_judges: Mapping[str, Mapping[str, Any]],
    eval_policy: Mapping[str, Any],
    candidate_config: Mapping[str, Any],
    ctx: Any,
) -> dict[str, Any]:
    objective = plan.get('objective') if isinstance(plan.get('objective'), Mapping) else {}
    required = [_text(item) for item in objective.get('validation_case_ids') or [] if _text(item)]
    selected = {case_id: cases[case_id] for case_id in required if case_id in cases}
    missing_cases = [case_id for case_id in required if case_id not in cases]
    missing_baseline = [case_id for case_id in required if case_id in cases and case_id not in baseline_judges]
    if missing_cases or missing_baseline:
        return {
            'status': 'rejected',
            'accepted': False,
            'reason': 'validation_case_coverage_missing',
            'missing_cases': missing_cases,
            'missing_baseline_judges': missing_baseline,
            'case_ids': list(selected),
        }
    if not selected:
        return {'status': 'rejected', 'accepted': False, 'reason': 'no_validation_cases'}
    patch = {'status': 'verified', 'workspace_ref': str(root), 'diff': diff}
    service = candidate_service(candidate_config, patch, ctx)
    public_service = {key: value for key, value in service.items()
                      if key in PUBLIC_SERVICE_KEYS and key != 'healthcheck'}
    health = service.get('healthcheck') if isinstance(service.get('healthcheck'), Mapping) else {}
    public_service['healthcheck'] = {
        key: health.get(key)
        for key in ('status', 'type', 'message', 'algorithm_status', 'healthy_instances')
        if key in health
    }
    if service.get('status') != 'ready':
        return {'status': 'candidate_service_failed', 'accepted': False, 'reason': 'candidate_service_failed',
                'service': public_service, 'case_ids': list(selected)}
    answers, judges = {}, {}
    for case_id, case in selected.items():
        answer = candidate_rag_answer(case, service)
        answers[case_id] = answer
        judges[case_id] = candidate_judge_result(case, answer, eval_policy)
    answer_refs = {
        case_id: {'status': answer.get('status'), 'trace_id': answer.get('trace_id')}
        for case_id, answer in answers.items()
    }
    judge_refs = {
        case_id: {
            'quality_label': judge.get('quality_label'),
            'failure_type': judge.get('failure_type'),
            'retrieval_failure_type': judge.get('retrieval_failure_type'),
            'overall_score': judge.get('overall_score'),
            'answer_correctness': judge.get('answer_correctness'),
        }
        for case_id, judge in judges.items()
    }
    baseline_summary = summarize_eval(tuple(
        baseline_judges[case_id] for case_id in selected if case_id in baseline_judges
    ))
    candidate_summary = summarize_eval(tuple(judges[case_id] for case_id in selected))
    candidate_summary = candidate_summary | {'id': 'repair.candidate_eval_summary'}
    comparison = compare_abtest(baseline_summary, candidate_summary)
    try:
        analysis = build_analysis_from_answers(selected, answers, judges) | {'id': 'repair.candidate_analysis'}
    except Exception as exc:
        return {
            'status': 'rejected',
            'accepted': False,
            'reason': f'candidate_analysis_failed:{type(exc).__name__}',
            'case_ids': list(selected),
            'service': public_service,
            'candidate_answer_refs': answer_refs,
            'candidate_judge_refs': judge_refs,
            'candidate_eval_summary': candidate_summary,
            'comparison': comparison,
            'candidate_analysis_error': str(exc),
        }
    delta = _analysis_delta_from(plan, comparison, analysis, candidate_summary)
    accepted, reason = _candidate_gate(comparison, candidate_summary, delta)
    return {
        'status': 'accepted' if accepted else 'rejected',
        'accepted': accepted,
        'reason': reason,
        'case_ids': list(selected),
        'service': public_service,
        'candidate_answer_refs': answer_refs,
        'candidate_judge_refs': judge_refs,
        'candidate_eval_summary': candidate_summary,
        'comparison': comparison,
        'candidate_analysis': analysis,
        'analysis_delta': delta,
    }


def _analysis_delta_from(
    plan: Mapping[str, Any],
    comparison: Mapping[str, Any],
    analysis: Mapping[str, Any],
    candidate_summary: Mapping[str, Any],
) -> dict[str, Any]:
    selected = plan.get('selected_group') if isinstance(plan.get('selected_group'), Mapping) else {}
    target = set((plan.get('objective') or {}).get('target_cases') or [])
    baseline_target_count = len(set(selected.get('case_ids') or []) & target) or len(target)
    rows = [row for row in analysis.get('rows') or [] if isinstance(row, Mapping) and row.get('case_id') in target]
    target_badcases = [row for row in rows if _text(row.get('issue_type')) != 'correct']
    remaining = [
        row for row in rows
        if _text(row.get('affected_block')) == _text(selected.get('function_block_id'))
        and _text(row.get('failure_mode')) == _text(selected.get('failure_mode'))
        and _text(row.get('issue_type')) != 'correct'
    ]
    old_groups = {
        (
            _text(group.get('function_block_id')),
            _text(group.get('issue_type')),
            _text(group.get('failure_mode')),
            _text(group.get('trace_signature')),
        )
        for group in plan.get('repair_group_queue') or []
        if isinstance(group, Mapping)
    }
    new_groups = [
        group for group in analysis.get('repair_group_queue') or []
        if isinstance(group, Mapping)
        and (
            _text(group.get('function_block_id')),
            _text(group.get('issue_type')),
            _text(group.get('failure_mode')),
            _text(group.get('trace_signature')),
        ) not in old_groups
    ]
    metrics = comparison.get('metrics') if isinstance(comparison.get('metrics'), Mapping) else {}
    delta = metrics.get('delta') if isinstance(metrics.get('delta'), Mapping) else {}
    resolved = not remaining and not target_badcases
    improved = len(remaining) < baseline_target_count and len(target_badcases) <= len(remaining)
    status = 'resolved' if resolved else 'improved' if improved else 'unchanged'
    if comparison.get('goodcase_guard', {}).get('status') == 'failed':
        status = 'regressed'
    return {
        'status': 'completed',
        'target_group_status': status,
        'target_remaining_badcase_count': len(remaining),
        'target_badcase_count': len(target_badcases),
        'target_total': len(target),
        'new_group_count': len(new_groups),
        'new_groups': new_groups[:5],
        'metric_delta': delta,
        'execution_failures': candidate_summary.get('execution_failures') or [],
        'recommended_action': 'accept_patch' if resolved or improved else 'rollback_to_baseline',
    }


def _candidate_gate(
    comparison: Mapping[str, Any],
    candidate_summary: Mapping[str, Any],
    delta: Mapping[str, Any],
) -> tuple[bool, str]:
    metrics = delta.get('metric_delta') if isinstance(delta.get('metric_delta'), Mapping) else {}
    if comparison.get('status') != 'completed':
        return False, _text(comparison.get('verdict')) or 'comparison_not_completed'
    if candidate_summary.get('execution_failures'):
        return False, 'candidate_execution_failed'
    if comparison.get('goodcase_guard', {}).get('status') == 'failed':
        return False, 'goodcase_guard_failed'
    if delta.get('new_group_count'):
        return False, 'new_analysis_group_detected'
    if delta.get('target_group_status') not in {'resolved', 'improved'}:
        return False, 'target_group_not_improved'
    if float(metrics.get('answer_correctness') or metrics.get('overall_score') or 0.0) < 0.0001:
        return False, 'metric_not_improved'
    return True, 'target_group_improved'


def _patch_base_decision(
    pre: Mapping[str, Any],
    candidate: Mapping[str, Any],
    delta: Mapping[str, Any],
    best: Mapping[str, Any],
) -> dict[str, Any]:
    if pre.get('status') != 'passed':
        return {'action': 'rollback_to_baseline', 'reason': _text(pre.get('reason')) or 'pre_validation_failed'}
    if candidate.get('accepted'):
        return {'action': 'accept_patch', 'reason': candidate.get('reason') or 'accepted'}
    if candidate.get('status') == 'candidate_service_failed':
        service = candidate.get('service') if isinstance(candidate.get('service'), Mapping) else {}
        health = service.get('healthcheck') if isinstance(service.get('healthcheck'), Mapping) else {}
        htype = _text(health.get('type'))
        external = {
            'ConnectError',
            'ConnectTimeout',
            'HTTPStatusError',
            'PoolTimeout',
            'ReadTimeout',
            'RemoteProtocolError',
            'TimeoutError',
        }
        if htype in external:
            return {'action': 'blocked', 'reason': f'external candidate service unavailable: {htype}'}
        return {'action': 'rollback_to_baseline', 'reason': 'candidate_service_failed'}
    failures = candidate.get('candidate_eval_summary', {}).get('execution_failures', [])
    if any(
        _text(row.get('failure_type')).startswith('candidate_route_')
        or _text(row.get('reason')).startswith('candidate_route_')
        for row in failures
    ):
        return {'action': 'blocked', 'reason': 'external candidate routing unavailable'}
    if _text(candidate.get('reason')).startswith('candidate_analysis_failed:'):
        return {'action': 'blocked', 'reason': candidate.get('reason')}
    metrics = delta.get('metric_delta') if isinstance(delta.get('metric_delta'), Mapping) else {}
    positive_metric = float(metrics.get('answer_correctness') or metrics.get('overall_score') or 0.0) > 0.0
    if delta.get('target_group_status') == 'improved' and not delta.get('new_group_count') and positive_metric:
        return {'action': 'continue_current_patch', 'reason': 'target topology improved below acceptance threshold'}
    if best and (best.get('analysis_delta') or {}).get('target_group_status') == 'improved':
        best_delta = best.get('analysis_delta') if isinstance(best.get('analysis_delta'), Mapping) else {}
        best_candidate = (
            best.get('candidate_validation') if isinstance(best.get('candidate_validation'), Mapping) else {}
        )
        best_pre = best.get('pre_validation') if isinstance(best.get('pre_validation'), Mapping) else {}
        best_metrics = best_delta.get('metric_delta') if isinstance(best_delta.get('metric_delta'), Mapping) else {}
        if (
            best_pre.get('status') == 'passed'
            and best_delta.get('status') == 'completed'
            and best_candidate.get('reason') == 'metric_not_improved'
            and not best_delta.get('new_group_count')
            and best_delta.get('target_group_status') == 'improved'
            and float(best_metrics.get('answer_correctness') or best_metrics.get('overall_score') or 0.0) > 0.0
        ):
            return {'action': 'fork_from_best_attempt', 'reason': 'previous attempt has best target topology'}
    return {'action': 'rollback_to_baseline', 'reason': candidate.get('reason') or 'candidate_rejected'}


def _pre_validate(
    root: Path,
    diff_info: Mapping[str, Any],
    plan: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    diff, files = diff_info.get('diff') or '', list(diff_info.get('files') or [])
    if not diff.strip():
        return {'status': 'failed', 'reason': 'empty_diff', 'diff_scope': {}, 'commands': []}
    scope = _diff_scope(files, plan)
    hardcode = _hardcode_check(diff, plan)
    if scope['status'] != 'passed' or hardcode['status'] != 'passed':
        reason = scope['reason'] if scope['status'] != 'passed' else hardcode['reason']
        return {'status': 'failed', 'reason': reason, 'diff_scope': scope, 'hardcode_check': hardcode,
                'commands': []}
    commands = _verify(root, policy)
    status = (
        'passed'
        if scope['status'] == hardcode['status'] == commands['status'] == 'passed'
        else 'failed'
    )
    reason = '' if status == 'passed' else next(
        item['reason'] for item in (scope, hardcode, commands) if item['status'] != 'passed'
    )
    return {'status': status, 'reason': reason, 'diff_scope': scope, 'hardcode_check': hardcode,
            'commands': commands['results']}


def _opencode_failure(run: Any) -> dict[str, Any]:
    error = run.last_error or {'type': 'process_failed', 'message': f'opencode exited with {run.returncode}'}
    return {
        'status': 'failed',
        'reason': f"opencode_failed:{_text(error.get('type'))}",
        'diff_scope': {},
        'hardcode_check': {},
        'commands': [],
        'opencode_error': error,
    }


def _diff_scope(files: list[str], plan: Mapping[str, Any]) -> dict[str, Any]:
    brief = plan.get('brief') if isinstance(plan.get('brief'), Mapping) else {}
    allowed, blocked, violations = [], [], []
    for raw_root in brief.get('allowed_roots') or []:
        root = posixpath.normpath(str(raw_root or '').strip())
        parts = root.split('/')
        if (
            root and not root.startswith('/') and '\\' not in root
            and '' not in parts and '.' not in parts and '..' not in parts
        ):
            allowed.append(root)
        else:
            violations.append(str(raw_root))
    for raw_root in brief.get('blocked_roots') or []:
        root = posixpath.normpath(str(raw_root or '').strip())
        parts = root.split('/')
        if root and not root.startswith('/') and '\\' not in root and '.' not in parts and '..' not in parts:
            blocked.append(root)
        else:
            violations.append(str(raw_root))
    for path in files:
        raw = str(path or '').strip()
        parts = raw.strip('/').split('/')
        normalized = posixpath.normpath(raw)
        invalid = (
            not normalized
            or normalized.startswith('/')
            or normalized == '.'
            or '\\' in raw
            or '' in parts
            or '.' in parts
            or '..' in parts
            or '\\' in normalized
        )
        in_allowed = any(normalized == root or normalized.startswith(f'{root}/') for root in allowed)
        in_blocked = any(normalized == root or normalized.startswith(f'{root}/') for root in blocked)
        if invalid or not in_allowed or in_blocked:
            violations.append(path)
    return {'status': 'passed' if not violations else 'failed', 'reason': 'diff_scope_violation',
            'violations': violations, 'allowed_roots': allowed, 'blocked_roots': blocked}


def _hardcode_check(diff: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    case_ids = set((plan.get('objective') or {}).get('validation_case_ids') or [])
    added = '\n'.join(line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
    hits = sorted(case_id for case_id in case_ids if case_id and case_id in added)
    return {'status': 'passed' if not hits else 'failed', 'reason': 'hard_coded_validation_case_id', 'hits': hits}


def _verify(root: Path, policy: Mapping[str, Any]) -> dict[str, Any]:
    results = []
    raw_commands = policy.get('verification_commands')
    commands = (
        raw_commands
        if isinstance(raw_commands, (list, tuple))
        else (() if raw_commands in (None, '') else (raw_commands,))
    )
    for raw in commands or DEFAULT_VERIFY:
        command = shlex.split(raw) if isinstance(raw, str) else [str(item) for item in raw]
        try:
            done = subprocess.run(command, cwd=str(root), capture_output=True, text=True, timeout=120, check=False)
            results.append({'command': command, 'returncode': done.returncode, 'stdout': done.stdout[-2000:],
                            'stderr': done.stderr[-2000:]})
        except Exception as exc:
            results.append({'command': command, 'returncode': None, 'stdout': '', 'stderr': str(exc),
                            'error_type': type(exc).__name__})
            return {'status': 'failed', 'reason': 'verification_command_failed', 'results': results}
        if results[-1]['returncode'] != 0:
            return {'status': 'failed', 'reason': 'verification_command_failed', 'results': results}
    return {'status': 'passed', 'reason': '', 'results': results}


def _task_card(plan: Mapping[str, Any], workspace: Mapping[str, Any], attempt: int,
               attempts: list[Mapping[str, Any]]) -> dict[str, Any]:
    brief = plan.get('brief') if isinstance(plan.get('brief'), Mapping) else {}
    return {
        'mode': 'lazyrag_trace_driven_repair_v1',
        'attempt': attempt,
        'objective': plan.get('objective'),
        'brief': brief,
        'workspace': {'path': workspace.get('workspace_ref'), 'source_dir': workspace.get('source_dir')},
        'previous_attempts': [
            {
                'attempt': item.get('attempt'),
                'decision': item.get('patch_base_decision'),
                'analysis_delta': item.get('analysis_delta'),
                'files_changed': item.get('files_changed'),
            }
            for item in attempts[-2:]
        ],
        'instructions': [
            'Patch exactly the selected function block. Do not choose another group.',
            'Inspect seed_files first and stay inside allowed_roots.',
            'Do not edit eval, analysis, dataset, tests, secrets, generated data, or vendored lazyllm.',
            'Make the smallest code change that addresses the trace-derived symptom.',
            'Run the requested verification command before stopping.',
        ],
        'stop_condition': 'Leave a minimal git diff in the workspace, or explain why no safe patch exists.',
    }


def _attempt_events(attempt: int, trace: Mapping[str, Any], pre: Mapping[str, Any], candidate: Mapping[str, Any],
                    delta: Mapping[str, Any], decision: Mapping[str, Any]) -> list[dict[str, Any]]:
    events = list(trace.get('projected_events') or [])
    for phase, value in (('pre_validation', pre), ('candidate_eval', candidate),
                         ('candidate_analysis', delta), ('decision', decision)):
        events.append({'event_id': f'repair_{attempt}_{phase}', 'phase': phase, 'source': 'repair',
                       'kind': phase, 'status': value.get('status') or value.get('action') or 'completed',
                       'severity': 'info', 'title': phase.replace('_', ' '),
                       'summary': value.get('reason') or value.get('target_group_status') or value.get('action') or ''})
    return events


def _result(status: str, plan: Mapping[str, Any], workspace: Mapping[str, Any], attempts: list[Mapping[str, Any]],
            best: Mapping[str, Any], message: str) -> dict[str, Any]:
    return {
        'id': 'repair.loop_result',
        'status': status,
        'message': message,
        'attempt_count': len(attempts),
        'attempts': attempts,
        'best_attempt': best.get('attempt'),
        'best_attempt_status': best.get('status'),
        'workspace_ref': workspace.get('workspace_ref'),
        'selected_group': plan.get('selected_group') or {},
        'events': [event for attempt in attempts for event in attempt.get('events', [])],
    }


def _workspace_path(policy: Mapping[str, Any], plan: Mapping[str, Any]) -> Path:
    base = (Path(os.getenv('LAZYMIND_EVO_BASE_DIR') or '/var/lib/lazymind/evo') / 'work' / 'repair').resolve()
    namespace = ''.join(
        ch if ch.isalnum() or ch in '._-' else '_'
        for ch in _text(policy.get('workspace_namespace') or policy.get('thread_id') or 'shared')
    ).strip('._-') or 'shared'
    digest = hashlib.sha1(json.dumps(plan.get('objective') or {}, sort_keys=True).encode()).hexdigest()[:12]
    if policy.get('candidate_workdir'):
        workspace = Path(_text(policy['candidate_workdir'])).resolve()
        expected = base / namespace / digest / 'candidate'
        if workspace != expected:
            raise RuntimeError(f'candidate workspace must match managed repair candidate path: {expected}')
        return workspace
    return base / namespace / digest / 'candidate'


def _prepare_workspace(source: Path, workspace: Path, objective_hash: str = '') -> None:
    if not (source / 'lazymind' / 'chat' / 'app.py').exists():
        raise RuntimeError(f'candidate source is not LazyRAG algorithm dir: {source}')
    source, workspace = source.resolve(), workspace.resolve()
    if source == workspace or source in workspace.parents or workspace in source.parents:
        raise RuntimeError(f'candidate workspace must be outside source tree: {workspace}')
    fingerprint = _source_fingerprint(source)
    if objective_hash:
        fingerprint['objective_hash'] = objective_hash
    if workspace.exists() and _workspace_fingerprint(workspace) != fingerprint:
        shutil.rmtree(workspace)
    created = not workspace.exists()
    if created:
        _copy_source(source, workspace)
    elif (workspace / '.git').exists():
        _reset_workspace(workspace)
    if not (workspace / 'lazymind' / 'chat' / 'app.py').exists():
        raise RuntimeError(f'candidate workspace is not LazyRAG algorithm dir: {workspace}')
    _ensure_git(workspace, created)
    _write_workspace_fingerprint(workspace, fingerprint)
    _reset_workspace(workspace)


def _copy_source(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns('.git', '.evo_repair_logs', '__pycache__', '*.pyc')
    for name in ('lazymind', 'chat', 'common', 'vocab', 'parsing', 'processor'):
        if (source / name).exists():
            shutil.copytree(source / name, target / name, ignore=ignore, dirs_exist_ok=True)
    for name in ('.dockerignore', 'Dockerfile', 'config.py', 'requirements.txt'):
        if (source / name).exists():
            shutil.copy2(source / name, target / name)


def _algorithm_source_root(value: Any) -> Path:
    path = Path(_text(value)).resolve()
    for candidate in (path, *path.parents):
        if (candidate / 'lazymind' / 'chat' / 'app.py').exists():
            return candidate
    return path


def _ensure_git(workspace: Path, created: bool) -> None:
    if not (workspace / '.git').exists():
        _git(workspace, 'init')
    if _git_code(workspace, 'rev-parse', '--verify', 'HEAD'):
        _git(workspace, 'add', '.')
        _git(workspace, '-c', 'user.email=evo@example.local', '-c', 'user.name=evo', 'commit', '-m', 'baseline')
    elif _git_code(workspace, 'diff', '--quiet', '--') and created:
        _git(workspace, 'add', '.')
        _git(workspace, '-c', 'user.email=evo@example.local', '-c', 'user.name=evo', 'commit', '-m', 'repair baseline')
    elif _git_code(workspace, 'diff', '--quiet', '--'):
        raise RuntimeError(f'existing repair workspace has dirty tracked files: {workspace}')


def _reset_workspace(workspace: Path) -> None:
    _git(workspace, 'reset', '--hard', 'HEAD')
    _git(workspace, 'clean', '-fd', '-e', '.evo_repair_logs', '--', '.')


def _diff(workspace: Path) -> dict[str, Any]:
    untracked = [path for path in _git(workspace, 'ls-files', '--others', '--exclude-standard').splitlines()
                 if path and path != 'opencode.json'
                 and not path.startswith('.evo_repair_logs/') and not path.endswith('.pyc')]
    if untracked:
        _git(workspace, 'add', '-N', '--', *untracked)
    return {'diff': _git(workspace, 'diff', '--'), 'files': _git(workspace, 'diff', '--name-only').splitlines()}


def _apply_diff(workspace: Path, diff: str) -> None:
    if not diff.strip():
        return
    subprocess.run(['git', '-C', str(workspace), 'apply', '-'], input=diff, text=True,
                   capture_output=True, timeout=60, check=True)


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(['git', '-c', f'safe.directory={workspace}', '-C', str(workspace), *args],
                            capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def _git_code(workspace: Path, *args: str) -> int:
    return subprocess.run(['git', '-c', f'safe.directory={workspace}', '-C', str(workspace), *args],
                          capture_output=True, text=True, timeout=60, check=False).returncode


def _opencode_env(policy: Mapping[str, Any]) -> dict[str, str]:
    llm_config = policy.get('llm_config') if isinstance(policy.get('llm_config'), Mapping) else {}
    role = {}
    value = llm_config.get('evo_llm')
    if isinstance(value, Mapping):
        role = value
    elif isinstance(value, list):
        role = next((item for item in value if isinstance(item, Mapping)), {})
    if not role:
        return {}
    model = _text(role.get('model') or role.get('name'))
    base_url = _text(role.get('base_url') or role.get('url')).rstrip('/')
    provider = ''.join(
        ch.lower() if ch.isalnum() else '_'
        for ch in _text(role.get('provider') or role.get('source'))
    ).strip('_')
    api_key = _text(role.get('api_key'))
    skip_auth = str(role.get('skip_auth')).lower() == 'true' or role.get('skip_auth') is True
    if not (provider and model and base_url and (api_key or skip_auth)):
        return {}
    key_env = 'OPENCODE_CORE_LLM_API_KEY'
    return {
        'OPENCODE_MODEL': f'{provider}/{model}',
        'OPENCODE_PROVIDER': provider,
        'OPENCODE_PROVIDER_MODEL': model,
        'OPENCODE_PROVIDER_LABEL': provider,
        'OPENCODE_PROVIDER_BASE_URL': base_url,
        'OPENCODE_PROVIDER_KEY_ENV': key_env,
        key_env: api_key or 'unused',
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
        'opencode_first_response_timeout_s',
        'llm_config',
    }
    return {
        **{key: safe[key] for key in safe_keys if key in safe},
        **{key: raw[key] for key in runtime_keys if key in raw},
    }


def _source_fingerprint(source: Path) -> dict[str, str]:
    return {'source_dir': str(source), 'source_hash': _tree_hash(source)}


def _workspace_fingerprint(workspace: Path) -> dict[str, str]:
    try:
        value = json.loads((workspace / '.git' / 'evo_repair_source.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return {}


def _write_workspace_fingerprint(workspace: Path, value: Mapping[str, str]) -> None:
    (workspace / '.git' / 'evo_repair_source.json').write_text(
        json.dumps(dict(value), sort_keys=True),
        encoding='utf-8',
    )


def _tree_hash(source: Path) -> str:
    digest = hashlib.sha1()
    for path in sorted(source.rglob('*')):
        if path.is_file() and not any(part in {'.git', '__pycache__'} for part in path.parts):
            rel = path.relative_to(source).as_posix()
            content = path.read_bytes()
            digest.update(rel.encode())
            digest.update(b'\0')
            digest.update(content)
    return digest.hexdigest()


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _text(value: Any) -> str:
    return str(value or '').strip()


def _validated_attempt(attempt: Mapping[str, Any]) -> bool:
    candidate = attempt.get('candidate_validation') if isinstance(attempt.get('candidate_validation'), Mapping) else {}
    trace = attempt.get('opencode_trace') if isinstance(attempt.get('opencode_trace'), Mapping) else {}
    pre = attempt.get('pre_validation') if isinstance(attempt.get('pre_validation'), Mapping) else {}
    analysis = attempt.get('analysis_delta') if isinstance(attempt.get('analysis_delta'), Mapping) else {}
    comparison = candidate.get('comparison') if isinstance(candidate.get('comparison'), Mapping) else {}
    return (
        attempt.get('status') == 'validated'
        and bool(_text(attempt.get('diff')))
        and pre.get('status') == 'passed'
        and candidate.get('accepted') is True
        and comparison.get('status') == 'completed'
        and analysis.get('status') == 'completed'
        and trace.get('returncode') == 0
        and not trace.get('last_error')
    )
