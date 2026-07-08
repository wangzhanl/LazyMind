from __future__ import annotations

import ast
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .code_index import DOMAIN_ROOTS
from .trace import safe_emit

DEFAULT_VERIFY = ('python -m compileall -q lazymind/chat lazymind/parsing',)
PATCH_BYTE_LIMIT = 64 * 1024
SECRET_LITERAL = re.compile(
    r'(?i)[\'"]?(api[_-]?key|token|secret|password|authorization)[\'"]?\s*[:=]\s*'
    r'([\'"]?)(?!<redacted>|unused\b|os\.getenv\b|getenv\b)[A-Za-z0-9._~+/=-]{8,}\2'
)


def pre_validate(
    root: Path,
    diff_info: Mapping[str, Any],
    plan: Mapping[str, Any],
    policy: Mapping[str, Any],
    trace: Any | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    safe_emit(trace, 'verify.pre_validation_started', status='started', attempt=attempt)
    diff, files = diff_info.get('diff') or '', list(diff_info.get('files') or [])
    if not diff.strip():
        safe_emit(trace, 'verify.pre_validation_completed', status='failed', attempt=attempt,
              payload={'reason': 'empty_diff'})
        return {'status': 'failed', 'reason': 'empty_diff', 'diff_scope': {}, 'commands': []}
    scope = _diff_scope(files, plan)
    safe_emit(trace, 'verify.diff_scope_completed', status='completed' if scope['status'] == 'passed' else 'failed',
          attempt=attempt, payload=scope)
    hardcode = _hardcode_check(diff, plan)
    safe_emit(trace, 'verify.hardcode_check_completed',
          status='completed' if hardcode['status'] == 'passed' else 'failed', attempt=attempt, payload=hardcode)
    patch_safety = _patch_safety_check(diff, policy)
    behavior = _behaviorful_check(root, diff, files)
    safe_emit(trace, 'verify.behaviorful_diff_completed',
          status='completed' if behavior['status'] == 'passed' else 'failed', attempt=attempt, payload=behavior)
    if (
        scope['status'] != 'passed'
        or hardcode['status'] != 'passed'
        or patch_safety['status'] != 'passed'
        or behavior['status'] != 'passed'
    ):
        reason = next(
            item['reason'] for item in (scope, hardcode, patch_safety, behavior) if item['status'] != 'passed'
        )
        safe_emit(trace, 'verify.pre_validation_completed', status='failed', attempt=attempt,
              payload={'reason': reason})
        return {'status': 'failed', 'reason': reason, 'diff_scope': scope, 'hardcode_check': hardcode,
                'patch_safety': patch_safety, 'behaviorful_check': behavior, 'commands': []}
    commands = _verify(root, policy, trace, attempt)
    status = (
        'passed'
        if scope['status'] == hardcode['status'] == commands['status'] == 'passed'
        else 'failed'
    )
    reason = '' if status == 'passed' else next(
        item['reason'] for item in (scope, hardcode, commands) if item['status'] != 'passed'
    )
    safe_emit(trace, 'verify.pre_validation_completed', status='completed' if status == 'passed' else 'failed',
          attempt=attempt, payload={'outcome': status, 'reason': reason})
    return {'status': status, 'reason': reason, 'diff_scope': scope, 'hardcode_check': hardcode,
            'patch_safety': patch_safety, 'behaviorful_check': behavior, 'commands': commands['results']}


def _diff_scope(files: list[str], plan: Mapping[str, Any]) -> dict[str, Any]:
    brief = plan.get('brief') if isinstance(plan.get('brief'), Mapping) else {}
    violations: list[Any] = []
    allowed = _scope_roots(brief.get('allowed_roots') or DOMAIN_ROOTS, violations)
    blocked = _scope_roots(brief.get('blocked_roots'), violations)
    file_paths = [(path, _relative_path(path)) for path in files]
    for path, normalized in file_paths:
        in_allowed = any(normalized == root or normalized.startswith(f'{root}/') for root in allowed)
        in_blocked = any(normalized == root or normalized.startswith(f'{root}/') for root in blocked)
        if not normalized or not in_allowed or in_blocked:
            violations.append(path)
    return {'status': 'passed' if not violations else 'failed', 'reason': 'diff_scope_violation',
            'violations': violations, 'allowed_roots': allowed, 'blocked_roots': blocked}


def _hardcode_check(diff: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    case_ids = set((plan.get('objective') or {}).get('validation_case_ids') or [])
    added = '\n'.join(line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
    hits = sorted(case_id for case_id in case_ids if case_id and case_id in added)
    return {'status': 'passed' if not hits else 'failed', 'reason': 'hard_coded_validation_case_id', 'hits': hits}


def _patch_safety_check(diff: str, policy: Mapping[str, Any]) -> dict[str, Any]:
    limit = _int(policy.get('max_patch_bytes'), PATCH_BYTE_LIMIT, 4096, 2 * 1024 * 1024)
    added = '\n'.join(line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
    size = len(diff.encode('utf-8'))
    leaked = sorted({match.group(1).lower() for match in SECRET_LITERAL.finditer(added)})
    reason = 'patch_too_large' if size > limit else 'secret_literal_in_patch' if leaked else ''
    return {'status': 'failed' if reason else 'passed', 'reason': reason, 'bytes': size,
            'limit': limit, 'secret_keys': leaked}


def _behaviorful_check(root: Path, diff: str, files: list[str]) -> dict[str, Any]:
    py_files = [path for path in files if path.endswith('.py') and _in_domain(path)]
    if not py_files:
        return {'status': 'failed', 'reason': 'no_domain_python_change', 'files': files}
    if _only_trivial_added_lines(diff):
        return {'status': 'failed', 'reason': 'trivial_patch_only', 'files': py_files}
    if forbidden := _forbidden_added_lines(diff):
        return {'status': 'failed', 'reason': 'forbidden_trivial_construct', 'files': py_files,
                'hits': forbidden}
    changed = []
    for rel in py_files:
        current = root / rel
        try:
            new_tree = ast.parse(current.read_text(encoding='utf-8'), filename=rel)
        except (OSError, SyntaxError) as exc:
            return {'status': 'failed', 'reason': 'python_ast_parse_failed',
                    'files': py_files, 'error_type': type(exc).__name__}
        old_text = _git_show(root, rel)
        if old_text is None:
            changed.append(rel)
            continue
        try:
            old_tree = ast.parse(old_text, filename=rel)
        except SyntaxError:
            changed.append(rel)
            continue
        if ast.dump(old_tree, include_attributes=False) != ast.dump(new_tree, include_attributes=False):
            changed.append(rel)
    return {'status': 'passed' if changed else 'failed',
            'reason': '' if changed else 'ast_unchanged',
            'files': py_files, 'behaviorful_files': changed}


def _only_trivial_added_lines(diff: str) -> bool:
    added = [
        line[1:].strip() for line in diff.splitlines()
        if line.startswith('+') and not line.startswith('+++') and line[1:].strip()
    ]
    if not added:
        return True
    return all(
        line.startswith('#')
        or line.startswith(('import ', 'from '))
        or _trivial_statement(line)
        or re.fullmatch(r'[A-Z_]*REPAIR[A-Z_]*\s*=.*', line)
        for line in added
    )


def _forbidden_added_lines(diff: str) -> list[str]:
    added = [
        line[1:].strip() for line in diff.splitlines()
        if line.startswith('+') and not line.startswith('+++') and line[1:].strip()
    ]
    hits = []
    for line in added:
        if _marker_assignment(line):
            hits.append(line)
        elif _dead_branch(line):
            hits.append(line)
    return hits


def _trivial_statement(line: str) -> bool:
    try:
        tree = ast.parse(line)
    except SyntaxError:
        return False
    if len(tree.body) != 1:
        return False
    node = tree.body[0]
    return isinstance(node, ast.Pass) or (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and node.value.value is Ellipsis
    )


def _dead_branch(line: str) -> bool:
    match = re.match(r'if\s+(.+?)\s*:', line)
    if not match:
        return False
    try:
        expression = ast.parse(match.group(1), mode='eval').body
    except SyntaxError:
        return False
    return _static_false(expression)


def _static_false(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is False or node.value == 0
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return isinstance(node.operand, ast.Constant) and bool(node.operand.value) is True
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return any(_static_false(value) for value in node.values)
        if isinstance(node.op, ast.Or):
            return all(_static_false(value) for value in node.values)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'bool' and len(node.args) == 1:
        return _static_false(node.args[0])
    return False


def _marker_assignment(line: str) -> bool:
    try:
        tree = ast.parse(line)
    except SyntaxError:
        match = re.match(r'([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=', line)
        names = [match.group(1)] if match else []
    else:
        if len(tree.body) != 1:
            return False
        names = _assigned_names(tree.body[0])
    for name in names:
        tokens = {token for token in name.lower().split('_') if token}
        if tokens & {'marker', 'sentinel', 'placeholder', 'dummy', 'noop'}:
            return True
    return False


def _assigned_names(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Assign):
        return [name for target in node.targets for name in _target_names(target)]
    if isinstance(node, ast.AnnAssign):
        return _target_names(node.target)
    if isinstance(node, ast.AugAssign):
        return _target_names(node.target)
    return []


def _target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for item in node.elts for name in _target_names(item)]
    return []


def _in_domain(path: str) -> bool:
    return any(path == root or path.startswith(f'{root}/') for root in DOMAIN_ROOTS)


def _git_show(root: Path, path: str) -> str | None:
    result = subprocess.run(
        ['git', '-c', f'safe.directory={root}', '-C', str(root), 'show', f'HEAD:{path}'],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _verify(
    root: Path,
    policy: Mapping[str, Any],
    trace: Any | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    results = []
    raw_commands = policy.get('verification_commands')
    commands = (
        raw_commands
        if isinstance(raw_commands, (list, tuple))
        else (() if raw_commands in (None, '') else (raw_commands,))
    )
    for raw in commands or DEFAULT_VERIFY:
        command = shlex.split(raw) if isinstance(raw, str) else [str(item) for item in raw]
        if command and command[0] == 'python':
            command[0] = sys.executable
        label = ' '.join(command[:4])
        safe_emit(trace, 'verify.command_started', status='started', attempt=attempt, payload={'command': label})
        try:
            done = subprocess.run(command, cwd=str(root), capture_output=True, text=True, timeout=120, check=False)
            results.append({'command': command, 'returncode': done.returncode, 'stdout': done.stdout[-2000:],
                            'stderr': done.stderr[-2000:]})
        except Exception as exc:
            results.append({'command': command, 'returncode': None, 'stdout': '', 'stderr': str(exc),
                            'error_type': type(exc).__name__})
            safe_emit(trace, 'verify.command_completed', status='failed', attempt=attempt,
                  payload={'command': label, 'error_type': type(exc).__name__})
            return {'status': 'failed', 'reason': 'verification_command_failed', 'results': results}
        safe_emit(trace, 'verify.command_completed', status='completed' if done.returncode == 0 else 'failed',
              attempt=attempt, payload={'command': label, 'returncode': done.returncode})
        if results[-1]['returncode'] != 0:
            return {'status': 'failed', 'reason': 'verification_command_failed', 'results': results}
    return {'status': 'passed', 'reason': '', 'results': results}


def _relative_path(value: Any) -> str:
    raw = str(value or '').strip()
    parts = raw.strip('/').split('/')
    if (
        not raw
        or raw.startswith('/')
        or '\\' in raw
        or any(part in {'', '.', '..'} for part in parts)
    ):
        return ''
    return PurePosixPath(raw).as_posix()


def _scope_roots(values: Any, violations: list[Any]) -> list[str]:
    roots = []
    for value in values or []:
        if path := _relative_path(value):
            roots.append(path)
        else:
            violations.append(str(value))
    return roots


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))
