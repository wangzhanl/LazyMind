from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jsonpatch
from jsonpointer import JsonPointer, JsonPointerException
from jsonschema import Draft202012Validator
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from evo.artifact_runtime.kernel import ArtifactRef

from .schemas import ConfigPatchAction, ConfigValidationIssue

HTTP_URL = TypeAdapter(AnyHttpUrl)

SCHEMAS: dict[str, dict[str, Any]] = {
    'run_config': {
        'type': 'object',
        'required': ['thread_id', 'mode', 'num_case', 'inputs', 'llm_config'],
        'properties': {
            'thread_id': {'type': 'string', 'minLength': 1},
            'mode': {'enum': ['auto', 'interactive']},
            'num_case': {'type': 'integer', 'minimum': 1},
            'inputs': {'type': 'object'},
            'llm_config': {'type': 'object'},
        },
    },
    'source_config': {
        'type': 'object',
        'properties': {
            'kb_id': {'type': 'array'},
            'csv_data': {'type': 'array'},
            'target_case_count': {'type': 'integer', 'minimum': 1},
            'min_case_count': {'type': 'integer', 'minimum': 1},
        },
    },
    'target_config': {
        'type': 'object',
        'required': ['target_chat_url', 'llm_config'],
        'properties': {
            'target_chat_url': {'type': 'string'},
            'llm_config': {'type': 'object'},
            'case_deadline_seconds': {'type': 'number', 'exclusiveMinimum': 0},
        },
    },
    'candidate_config': {
        'type': 'object',
        'required': ['target_chat_url', 'llm_config'],
        'properties': {
            'target_chat_url': {'type': 'string'},
            'llm_config': {'type': 'object'},
            'case_deadline_seconds': {'type': 'number', 'exclusiveMinimum': 0},
        },
    },
    'eval_policy': {
        'type': 'object',
        'required': ['judge_llm_config'],
        'properties': {'judge_llm_config': {'type': 'object'}},
    },
    'repair_policy': {
        'type': 'object',
        'required': ['llm_config'],
        'properties': {'llm_config': {'type': 'object'}, 'workspace_namespace': {'type': 'string'}},
    },
}


@dataclass(frozen=True)
class ConfigPatchOk:
    ref: ArtifactRef
    pointer: str
    value: Any
    preview: dict[str, Any]


class ConfigValidationError(ValueError):
    def __init__(self, issues: list[ConfigValidationIssue]) -> None:
        self.issues = issues
        super().__init__('; '.join(issue.message for issue in issues))


def validate_config_patch(thread_id: str, action: ConfigPatchAction, ref: ArtifactRef,
                          current: object) -> ConfigPatchOk:
    issues = _pointer_issues(action.pointer)
    patched = current
    before = None
    if not issues:
        try:
            before = JsonPointer(action.pointer).resolve(current)
            patched = jsonpatch.apply_patch(
                current,
                [{'op': 'replace', 'path': action.pointer, 'value': action.value}],
                in_place=False,
            )
        except (jsonpatch.JsonPatchException, JsonPointerException) as exc:
            issues.append(_issue(action.pointer, 'unknown_field', f'path does not exist: {action.pointer}', exc))
    issues.extend(_schema_issues(action.target, patched))
    issues.extend(_semantic_issues(thread_id, action.target, patched))
    if issues:
        raise ConfigValidationError(issues)
    return ConfigPatchOk(ref, action.pointer, action.value, {
        'target': action.target,
        'pointer': action.pointer,
        'before': _summary(before),
        'after': _summary(action.value),
    })


def _pointer_issues(pointer: str) -> list[ConfigValidationIssue]:
    try:
        target = JsonPointer(pointer)
    except JsonPointerException as exc:
        return [_issue(pointer, 'invalid_type', f'invalid JSON pointer: {exc}', pointer)]
    if not target.parts:
        return [_issue(pointer, 'immutable_field', 'root config replacement is not allowed')]
    if target.parts[-1] == '-':
        return [_issue(pointer, 'invalid_type', 'array append is not supported')]
    return []


def _schema_issues(target: str, value: object) -> list[ConfigValidationIssue]:
    issues = []
    for error in Draft202012Validator(SCHEMAS[target]).iter_errors(value):
        path = JsonPointer.from_parts(error.absolute_path).path
        code = 'missing_required' if error.validator == 'required' else 'invalid_type'
        issues.append(_issue(path, code, error.message))
    return issues


def _semantic_issues(thread_id: str, target: str, value: object) -> list[ConfigValidationIssue]:
    if not isinstance(value, Mapping):
        return []
    issues = []
    if target == 'run_config' and str(value.get('thread_id') or '') != thread_id:
        issues.append(_issue('/thread_id', 'immutable_field', 'run_config.thread_id is immutable'))
    if target == 'source_config' and not value.get('kb_id') and not value.get('csv_data'):
        issues.append(_issue('', 'missing_required', 'source_config requires kb_id or csv_data'))
    if target == 'source_config' and value.get('csv_data') and int(value.get('min_case_count') or 0) < 100:
        issues.append(_issue('/min_case_count', 'out_of_range', 'CSV source min_case_count must be >= 100'))
    if target in {'target_config', 'candidate_config'}:
        issues.extend(_url_issue('/target_chat_url', value.get('target_chat_url')))
        issues.extend(_role_issue('/llm_config/llm', value.get('llm_config'), 'llm'))
    if target == 'eval_policy':
        issues.extend(_role_issue('/judge_llm_config/evo_llm', value.get('judge_llm_config'), 'evo_llm'))
        issues.extend(_finite_issues(value))
    if target == 'repair_policy':
        issues.extend(_role_issue('/llm_config/evo_llm', value.get('llm_config'), 'evo_llm'))
        if str(value.get('workspace_namespace') or thread_id) != thread_id:
            issues.append(_issue('/workspace_namespace', 'cross_thread_reference',
                                 'workspace_namespace must stay within the thread'))
    return issues


def _url_issue(path: str, value: object) -> list[ConfigValidationIssue]:
    try:
        HTTP_URL.validate_python(value)
        return []
    except ValidationError as exc:
        return [_issue(path, 'invalid_url', f'{path} must be an http(s) URL', exc)]


def _role_issue(path: str, value: object, role: str) -> list[ConfigValidationIssue]:
    if isinstance(value, Mapping) and isinstance(value.get(role), Mapping):
        return []
    return [_issue(path, 'missing_required', f'{path} is required')]


def _finite_issues(value: object, path: str = '') -> list[ConfigValidationIssue]:
    if isinstance(value, Mapping):
        return [issue for key, item in value.items() for issue in _finite_issues(item, f'{path}/{key}')]
    if isinstance(value, float) and not math.isfinite(value):
        return [_issue(path, 'out_of_range', f'{path} must be finite', value)]
    return []


def _issue(path: str, code: str, message: str, value: object = '') -> ConfigValidationIssue:
    return ConfigValidationIssue(path=path or '/', code=code, message=message,
                                 observed_value_summary=_summary(value))


def _summary(value: object) -> str:
    if isinstance(value, Mapping):
        return '{' + ','.join(str(key) for key in list(value)[:6]) + '}'
    return str(value)[:200]
