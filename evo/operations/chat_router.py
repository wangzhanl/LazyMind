from __future__ import annotations

import asyncio
import ast
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote, urlparse, urlunparse
from uuid import uuid4

import httpx

DEFAULT_DISABLED_TOOLS = tuple(
    'temp_kb calculator wikipedia web_search academic_search url_fetch multimodal image_generator image_editor '
    'vocab_learn read_memory memory_editor skill_editor local_fs feishu notion'.split()
)
EVO_EVAL_MEMORY = (
    'Evo evaluation request: use the knowledge-base tool as the evidence source, '
    'keep retrieval bounded, answer directly when enough evidence is available, '
    'and do not ask the user or schedule tasks.'
)
DELTA_KEYS = ('delta', 'answer_delta', 'content_delta', 'text')
FINAL_ANSWER_KEYS = ('answer', 'message', 'text', 'result', 'content')
SOURCE_KEYS = ('sources', 'source_documents', 'retrieved_contexts', 'contexts', 'documents')
TOOL_RESULT = re.compile(r'<tool_result>(.*?)</tool_result>', re.S)
CONTROL_TAG = re.compile(r'<(?:tp|trp|tool_call|tool_result)(?:\s[^>]*)?>.*?</(?:tp|trp|tool_call|tool_result)>', re.S)
TRACE_ID = re.compile(r'^[0-9a-f]{32}$')
SSE_FIELD = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*:')
TOOL_ERROR_MARKERS = (
    '[tool error]',
    'moduleexecutionerror',
    'connectionreseterror',
    'connection broken',
    'readtimeout',
    'connecttimeout',
    'max retries exceeded',
    'httpconnectionpool',
)

DEFAULT_CASE_DEADLINE_SECONDS = 300.0
DEFAULT_FIRST_FRAME_TIMEOUT_SECONDS = 60.0
ROUTER_ADMIN_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class RouterChatRequest:
    router_chat_url: str
    router_admin_url: str
    algorithm_id: str
    query: str
    kb_ids: tuple[str, ...]
    trace_id: str = ''
    conversation_id: str = ''
    user_id: str = '0'
    history: tuple[Mapping[str, Any], ...] = ()
    llm_config: Mapping[str, Any] | None = None
    disabled_tools: tuple[str, ...] = DEFAULT_DISABLED_TOOLS
    connect_timeout_seconds: float = 5.0
    write_timeout_seconds: float = 60.0
    pool_timeout_seconds: float = 5.0
    case_deadline_seconds: float = DEFAULT_CASE_DEADLINE_SECONDS
    first_frame_timeout_seconds: float = DEFAULT_FIRST_FRAME_TIMEOUT_SECONDS


def call_router_chat(request: RouterChatRequest) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return _failed(
            {},
            _target_from_raw(request),
            'chat_runtime_error',
            'call async_call_router_chat from a running event loop',
        )

    return asyncio.run(async_call_router_chat(request))


async def async_call_router_chat(request: RouterChatRequest) -> dict[str, Any]:
    try:
        normalized = _normalize_request(request)
    except (TypeError, ValueError) as exc:
        return _failed({}, _target_from_raw(request), 'chat_config_error', str(exc))

    deadline = time.monotonic() + normalized.case_deadline_seconds
    timeout = httpx.Timeout(
        connect=normalized.connect_timeout_seconds,
        write=normalized.write_timeout_seconds,
        read=None,
        pool=normalized.pool_timeout_seconds,
    )

    try:
        return await asyncio.wait_for(
            _call_router_chat(normalized, timeout, deadline),
            timeout=normalized.case_deadline_seconds,
        )
    except asyncio.TimeoutError:
        return _failed(
            {},
            _target(normalized),
            'chat_timeout',
            'chat stream exceeded case deadline after 0 frame(s)',
        )
    except httpx.HTTPError as exc:
        return _failed({}, _target(normalized), 'chat_transport_error', str(exc))
    except Exception as exc:
        return _failed({}, _target(normalized), 'chat_unknown_error', f'{type(exc).__name__}: {exc}')


async def _call_router_chat(
    request: RouterChatRequest,
    timeout: httpx.Timeout,
    deadline: float,
) -> dict[str, Any]:
    target = _target(request)
    payload = _payload(request)
    stream: dict[str, Any] = {'frames': [], 'answer': '', 'finished': False}
    pending_data_lines: list[str] = []
    line_task: asyncio.Task[str] | None = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        algorithm_target = await _verify_algorithm(client, request, target)
        if isinstance(algorithm_target.get('chat_error'), Mapping):
            return algorithm_target
        target = algorithm_target
        async with client.stream(
            'POST',
            request.router_chat_url,
            json=payload,
            headers={'Accept': 'text/event-stream', 'Content-Type': 'application/json'},
        ) as response:
            if response.status_code != 200:
                return _failed(stream, target, 'chat_http_error', await _http_error(response))

            routed = response.headers.get('X-Algorithm-Id', '').strip()
            if not routed:
                return _failed(stream, target, 'router_header_missing', 'missing X-Algorithm-Id response header')
            target = target | {'routed_algorithm_id': routed}
            if routed != request.algorithm_id:
                return _failed(
                    stream,
                    target,
                    'router_algorithm_mismatch',
                    f'expected {request.algorithm_id}, got {routed}',
                )

            routed_instance = response.headers.get('X-Instance-Host', '').strip()
            if routed_instance:
                target = target | {'routed_instance_host': routed_instance}

            lines = response.aiter_lines()
            first_frame_deadline = time.monotonic() + request.first_frame_timeout_seconds
            try:
                while True:
                    remaining = _remaining_seconds(deadline, first_frame_deadline, bool(stream['frames']))
                    if remaining <= 0:
                        await response.aclose()
                        return _timeout_failure(stream, target)
                    if line_task is None:
                        line_task = asyncio.create_task(lines.__anext__())
                    done, _ = await asyncio.wait({line_task}, timeout=min(1.0, remaining))
                    if not done:
                        continue
                    try:
                        line = line_task.result()
                    except StopAsyncIteration:
                        if pending_data_lines:
                            accepted = _accept_payload(target, stream, '\n'.join(pending_data_lines))
                            if accepted is not None:
                                return accepted
                        return _normalize(target, stream)
                    line_task = None
                    payload_text = _sse_payload(str(line or ''), pending_data_lines)
                    if payload_text is None:
                        continue
                    accepted = _accept_payload(target, stream, payload_text)
                    if accepted is not None:
                        return accepted
            finally:
                if line_task is not None and not line_task.done():
                    line_task.cancel()
                    await asyncio.gather(line_task, return_exceptions=True)


def _normalize_request(request: RouterChatRequest) -> RouterChatRequest:
    router_chat_url = _stream_url(request.router_chat_url)
    router_admin_url = _base_url(request.router_admin_url, 'router_admin_url')
    algorithm_id = str(request.algorithm_id or '').strip()
    if not algorithm_id:
        raise ValueError('algorithm_id is required')
    query = str(request.query or '').strip()
    if not query:
        raise ValueError('query is required')
    kb_ids = tuple(dict.fromkeys(str(item).strip() for item in request.kb_ids if str(item).strip()))
    if not kb_ids:
        raise ValueError('kb_ids is required')
    trace_id = str(request.trace_id or uuid4().hex).strip().lower()
    if not TRACE_ID.fullmatch(trace_id):
        raise ValueError('trace_id must be a 32-character lowercase hex string')
    conversation_id = str(request.conversation_id or trace_id).strip()
    user_id = str(request.user_id or '0').strip() or '0'
    history = tuple(dict(item) for item in request.history)
    return replace(
        request,
        router_chat_url=router_chat_url,
        router_admin_url=router_admin_url,
        algorithm_id=algorithm_id,
        query=query,
        kb_ids=kb_ids,
        trace_id=trace_id,
        conversation_id=conversation_id,
        user_id=user_id,
        history=history,
        connect_timeout_seconds=_positive_number(request.connect_timeout_seconds, 'connect_timeout_seconds'),
        write_timeout_seconds=_positive_number(request.write_timeout_seconds, 'write_timeout_seconds'),
        pool_timeout_seconds=_positive_number(request.pool_timeout_seconds, 'pool_timeout_seconds'),
        case_deadline_seconds=_positive_number(request.case_deadline_seconds, 'case_deadline_seconds'),
        first_frame_timeout_seconds=_positive_number(
            request.first_frame_timeout_seconds,
            'first_frame_timeout_seconds',
        ),
    )


def _payload(request: RouterChatRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'algorithm_id': request.algorithm_id,
        'message': {
            'query': request.query,
            'history': list(request.history),
            'current_turn_seq': 1,
        },
        'conversation': {
            'session_id': request.trace_id,
            'conversation_id': request.conversation_id,
            'user_id': request.user_id,
            'mode': 'auto',
        },
        'retrieval': {'filters': {'kb_id': list(request.kb_ids)}},
        'runtime': {'debug': False, 'reasoning': True, 'trace': True},
        'personalization': {'use_memory': True, 'memory': EVO_EVAL_MEMORY},
        'agent': {
            'disabled_tools': list(request.disabled_tools),
            'available_skills': [],
            'has_subagents': False,
            'enable_subagent': False,
        },
        'plugin': {'enable_plugin': False},
    }
    if request.llm_config:
        payload['runtime']['llm_config'] = dict(request.llm_config)
    return payload


def _sse_payload(line: str, pending_data_lines: list[str]) -> str | None:
    text = line.rstrip('\r')
    if not text:
        if not pending_data_lines:
            return None
        payload = '\n'.join(pending_data_lines)
        pending_data_lines.clear()
        return payload
    if text.startswith(':'):
        return None
    if text.startswith('data:'):
        value = text[5:]
        pending_data_lines.append(value[1:] if value.startswith(' ') else value)
        return None
    if SSE_FIELD.match(text):
        return None
    return text


def _accept_payload(
    target: Mapping[str, Any],
    stream: dict[str, Any],
    payload_text: str,
) -> dict[str, Any] | None:
    text = payload_text.strip()
    if text == '[DONE]':
        stream['finished'] = True
        return _normalize(target, stream)
    try:
        frame = json.loads(text)
    except json.JSONDecodeError:
        return _failed(stream, target, 'chat_protocol_error', f'non-json SSE data: {text[:120]}')
    if not isinstance(frame, Mapping):
        return _failed(stream, target, 'chat_protocol_error', 'SSE JSON is not an object')

    stream['frames'].append(dict(frame))
    data = frame.get('data') if isinstance(frame.get('data'), Mapping) else frame
    codes = [value for value in (frame.get('code'), data.get('code')) if value is not None]
    status = str(data.get('status') or '').upper()
    if any(code not in (200, '200') for code in codes) or status == 'FAILED':
        message = data.get('msg') or frame.get('msg') or data.get('message') or status
        return _failed(stream, target, 'chat_business_error', str(message))
    stream['answer'] += str(next((data[key] for key in DELTA_KEYS if data.get(key)), ''))
    if status == 'FINISHED':
        stream['finished'] = True
        return _normalize(target, stream)
    return None


def _normalize(target: Mapping[str, Any], stream: Mapping[str, Any]) -> dict[str, Any]:
    frames = [
        frame.get('data') if isinstance(frame.get('data'), Mapping) else frame
        for frame in stream.get('frames', [])
        if isinstance(frame, Mapping)
    ]
    raw_answer = str(stream.get('answer') or _last(frames, FINAL_ANSWER_KEYS)).strip()
    if not stream.get('finished'):
        return _failed(stream, target, 'chat_protocol_error', 'stream ended before FINISHED')
    if not raw_answer:
        return _failed(stream, target, 'chat_protocol_error', 'stream finished without answer text')
    tool_errors = extract_chat_tool_errors(frames, raw_answer)
    sources = _sources(frames, raw_answer)
    contexts, doc_ids, chunk_ids = _source_refs(sources, target)
    answer = _answer_text(raw_answer)
    return {
        'status': 'ok',
        'answer': answer,
        'trace_id': str(_last(frames, ('trace_id',)) or target.get('trace_id') or ''),
        'algorithm_id': str(target.get('algorithm_id') or ''),
        'routed_algorithm_id': str(target.get('routed_algorithm_id') or ''),
        'routed_instance_host': str(target.get('routed_instance_host') or ''),
        'contexts': _unique(contexts),
        'doc_ids': _unique(doc_ids),
        'chunk_ids': _unique(chunk_ids),
        'sources': sources,
        'tool_errors': tool_errors,
        'frames': list(stream.get('frames') or []),
        'chat_error': None,
        'target': dict(target),
    }


def _failed(
    stream: Mapping[str, Any],
    target: Mapping[str, Any],
    error_type: str,
    message: str,
) -> dict[str, Any]:
    return {
        'status': 'failed',
        'answer': str(stream.get('answer') or ''),
        'trace_id': str(target.get('trace_id') or ''),
        'algorithm_id': str(target.get('algorithm_id') or ''),
        'routed_algorithm_id': str(target.get('routed_algorithm_id') or ''),
        'routed_instance_host': str(target.get('routed_instance_host') or ''),
        'contexts': [],
        'doc_ids': [],
        'chunk_ids': [],
        'sources': [],
        'tool_errors': [],
        'frames': list(stream.get('frames') or []),
        'chat_error': {'type': error_type, 'message': message},
        'target': dict(target),
    }


def _target(request: RouterChatRequest) -> dict[str, Any]:
    return {
        'router_chat_url': request.router_chat_url,
        'router_admin_url': request.router_admin_url,
        'algorithm_id': request.algorithm_id,
        'kb_id': ';'.join(request.kb_ids),
        'trace_id': request.trace_id,
        'conversation_id': request.conversation_id,
        'user_id': request.user_id,
    }


async def _verify_algorithm(
    client: httpx.AsyncClient,
    request: RouterChatRequest,
    target: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        response = await asyncio.wait_for(
            client.get(_algorithm_url(request.router_admin_url, request.algorithm_id)),
            timeout=ROUTER_ADMIN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        message = f'algorithm detail request exceeded {ROUTER_ADMIN_TIMEOUT_SECONDS:g}s'
        return _failed({}, target, 'router_algorithm_timeout', message)
    except httpx.HTTPError as exc:
        return _failed({}, target, 'router_algorithm_transport_error', str(exc))
    if response.status_code != 200:
        body = response.text.strip()
        suffix = f': {body[:300]}' if body else ''
        return _failed({}, target, 'router_algorithm_unavailable', f'HTTP {response.status_code}{suffix}')
    try:
        detail = response.json()
    except json.JSONDecodeError:
        return _failed({}, target, 'router_algorithm_protocol_error', 'algorithm detail is not JSON')
    if not isinstance(detail, Mapping):
        return _failed({}, target, 'router_algorithm_protocol_error', 'algorithm detail is not an object')
    instances = detail.get('instances') if isinstance(detail.get('instances'), list) else []
    healthy = [item for item in instances if isinstance(item, Mapping) and item.get('status') == 'healthy']
    if detail.get('status') != 'active' or not healthy:
        message = f'algorithm {request.algorithm_id} is not active with healthy instances'
        return _failed({}, target, 'router_algorithm_unhealthy', message)
    return dict(target) | {
        'algorithm_status': str(detail.get('status') or ''),
        'healthy_instances': len(healthy),
    }


def _target_from_raw(request: RouterChatRequest) -> dict[str, Any]:
    try:
        kb_id = ';'.join(str(item) for item in request.kb_ids)
    except TypeError:
        kb_id = str(request.kb_ids or '')
    return {
        'router_chat_url': str(request.router_chat_url or ''),
        'router_admin_url': str(request.router_admin_url or ''),
        'algorithm_id': str(request.algorithm_id or ''),
        'kb_id': kb_id,
        'trace_id': str(request.trace_id or ''),
        'conversation_id': str(request.conversation_id or ''),
        'user_id': str(request.user_id or ''),
    }


def _sources(frames: Sequence[Mapping[str, Any]], answer: str) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for data in frames:
        for key in SOURCE_KEYS:
            _collect_sources(data.get(key), sources)
        for key in ('tool_result', 'tool_results'):
            _collect_sources(data.get(key), sources)
        _collect_tool_result_sources(str(data.get('text') or ''), sources)
    for match in TOOL_RESULT.finditer(answer):
        _collect_sources(_parse_structured(match.group(1)), sources)
    return [dict(source) for source in sources]


def _collect_sources(value: Any, sources: list[dict[str, Any]]) -> None:
    value = _parse_structured(value)
    if isinstance(value, Mapping):
        if _source_like(value):
            sources.append(dict(value))
        for key in SOURCE_KEYS:
            _collect_sources(value.get(key), sources)
        items = value.get('items')
        if isinstance(items, list):
            for item in items:
                _collect_sources(item, sources)
        for key in ('result', 'data', 'tool_result', 'tool_results'):
            _collect_sources(value.get(key), sources)
    elif isinstance(value, list):
        for item in value:
            _collect_sources(item, sources)


def _source_like(value: Mapping[str, Any]) -> bool:
    metadata = value.get('global_metadata')
    metadata = metadata if isinstance(metadata, Mapping) else {}
    has_text = any(value.get(key) for key in ('content', 'text', 'chunk'))
    has_ref = any(value.get(key) for key in ('doc_id', 'docid', 'document_id', 'chunk_id', 'chunkid', 'uid', 'id'))
    return bool(has_text and (has_ref or metadata.get('docid') or metadata.get('core_document_id')))


def _collect_tool_result_sources(text: str, sources: list[dict[str, Any]]) -> None:
    for match in TOOL_RESULT.finditer(text or ''):
        _collect_sources(match.group(1), sources)


def _parse_structured(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in '{[':
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return value


def _answer_text(raw_answer: str) -> str:
    text = str(raw_answer or '').strip()
    if not text:
        return ''
    controls = list(CONTROL_TAG.finditer(text))
    cleaned = text[controls[-1].end():].strip() if controls else text
    cleaned = CONTROL_TAG.sub('', cleaned)
    return re.sub(r'\n{3,}', '\n\n', cleaned).strip()


def _source_refs(
    sources: Sequence[Mapping[str, Any]],
    target: Mapping[str, Any],
) -> tuple[list[Any], list[str], list[str]]:
    contexts: list[Any] = []
    doc_ids: list[str] = []
    chunk_ids: list[str] = []
    target_kbs = [item for item in str(target.get('kb_id') or '').split(';') if item]
    fallback_kb = target_kbs[0] if len(target_kbs) == 1 else ''
    for source in sources:
        metadata = source.get('global_metadata')
        metadata = metadata if isinstance(metadata, Mapping) else {}
        kb_id = str(
            source.get('kb_id')
            or source.get('dataset_id')
            or metadata.get('kb_id')
            or metadata.get('dataset_id')
            or fallback_kb
        ).strip()
        doc = str(
            source.get('doc_id')
            or source.get('docid')
            or source.get('document_id')
            or metadata.get('docid')
            or metadata.get('core_document_id')
            or ''
        ).strip()
        chunk = str(
            source.get('chunk_id')
            or source.get('chunkid')
            or source.get('segment_id')
            or source.get('segement_id')
            or source.get('uid')
            or source.get('id')
            or ''
        ).strip()
        doc_ref = doc if ':' in doc else f'{kb_id}:{doc}' if kb_id and doc else doc
        chunk_ref = chunk if ':' in chunk else f'{doc_ref}:{chunk}' if doc_ref and chunk else chunk
        contexts.append(source.get('content') or source.get('text') or source.get('chunk'))
        doc_ids.append(doc_ref)
        chunk_ids.append(chunk_ref)
    return contexts, doc_ids, chunk_ids


def extract_chat_tool_errors(frames: Sequence[Mapping[str, Any]], answer: str = '') -> list[Any]:
    errors = []
    for data in frames:
        for key in ('tool_error', 'tool_errors', 'kb_errors'):
            if data.get(key):
                errors.append(data[key])
        for key in ('tool_result', 'tool_results'):
            _collect_tool_errors(data.get(key), errors)
        _collect_tool_result_tags(str(data.get('text') or ''), errors)
    _collect_tool_result_tags(answer, errors)
    return errors


def _collect_tool_errors(value: Any, errors: list[Any]) -> None:
    parsed = _parse_structured(value)
    if parsed is not value:
        _collect_tool_errors(parsed, errors)
        return
    if isinstance(value, Mapping):
        if _mapping_is_tool_error(value):
            errors.append(dict(value))
            return
        for key in ('tool_error', 'tool_errors', 'kb_errors', 'error', 'exception', 'traceback'):
            if value.get(key):
                _collect_tool_errors(value[key], errors)
        result = value.get('result')
        if isinstance(result, str):
            _collect_tool_errors(result, errors)
        elif isinstance(result, Mapping) and _mapping_is_tool_error(result):
            errors.append(dict(result))
    elif isinstance(value, list):
        for item in value:
            _collect_tool_errors(item, errors)
    elif _looks_like_tool_error(value):
        errors.append(str(value))


def _collect_tool_result_tags(text: str, errors: list[Any]) -> None:
    for match in TOOL_RESULT.finditer(text or ''):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        _collect_tool_errors(payload, errors)


def _looks_like_tool_error(value: Any) -> bool:
    text = _error_text(value)
    return bool(text) and any(marker in text.lower() for marker in TOOL_ERROR_MARKERS)


def _mapping_is_tool_error(value: Mapping[str, Any]) -> bool:
    if value.get('success') is False:
        return True
    status = str(value.get('status') or '').strip().lower()
    if status in {'error', 'failed', 'fail'}:
        return True
    return _looks_like_tool_error(value.get('error') or value.get('exception') or value.get('traceback'))


def _error_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value or '').strip()


async def _http_error(response: httpx.Response) -> str:
    body = (await response.aread()).decode(errors='replace').strip()
    suffix = f': {body[:300]}' if body else ''
    return f'HTTP {response.status_code}{suffix}'


def _timeout_failure(stream: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    frames = [
        frame.get('data') if isinstance(frame.get('data'), Mapping) else frame
        for frame in stream.get('frames', [])
        if isinstance(frame, Mapping)
    ]
    if not frames:
        return _failed(stream, target, 'chat_timeout', 'chat stream exceeded first-frame deadline after 0 frame(s)')
    last = frames[-1]
    parts = [f'chat stream exceeded case deadline after {len(frames)} frame(s)']
    if last.get('status'):
        parts.append(f'last_status={last["status"]}')
    if last.get('code') is not None:
        parts.append(f'last_code={last["code"]}')
    if stream.get('answer'):
        parts.append(f'answer_chars={len(str(stream["answer"]))}')
    return _failed(stream, target, 'chat_timeout', '; '.join(parts))


def _remaining_seconds(deadline: float, first_frame_deadline: float, has_frames: bool) -> float:
    now = time.monotonic()
    remaining = deadline - now
    if not has_frames:
        remaining = min(remaining, first_frame_deadline - now)
    return remaining


def _stream_url(url: str) -> str:
    parsed = _parsed_http_url(url, 'router_chat_url')
    _ensure_path(parsed.path, 'router_chat_url', {'', '/', '/api/chat/stream'})
    return urlunparse((parsed.scheme, parsed.netloc, '/api/chat/stream', '', '', ''))


def _base_url(url: str, field: str) -> str:
    parsed = _parsed_http_url(url, field)
    _ensure_path(parsed.path, field, {'', '/'})
    return urlunparse((parsed.scheme, parsed.netloc, '', '', '', ''))


def _algorithm_url(router_admin_url: str, algorithm_id: str) -> str:
    parsed = urlparse(router_admin_url)
    path = f'/inner/algorithm/{quote(algorithm_id, safe="")}'
    return urlunparse((parsed.scheme, parsed.netloc, path, '', '', ''))


def _parsed_http_url(url: str, field: str):
    parsed = urlparse(str(url or '').strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError(f'{field} must be an http(s) URL')
    return parsed


def _ensure_path(path: str, field: str, allowed: set[str]) -> None:
    value = path.rstrip('/') if path not in {'', '/'} else path
    allowed_values = {item.rstrip('/') if item not in {'', '/'} else item for item in allowed}
    if value not in allowed_values:
        specific = sorted(item for item in allowed_values if item not in {'', '/'})
        suffix = f' or {specific[0]} URL' if specific else ''
        raise ValueError(f'{field} must be a router origin{suffix}')


def _positive_number(value: Any, field: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f'{field} must be a positive finite number')
    return number


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value or '').strip()))


def _last(items: Sequence[Mapping[str, Any]], keys: tuple[str, ...]) -> Any:
    return next((item[key] for item in reversed(items) for key in keys if item.get(key)), '')


__all__ = ['RouterChatRequest', 'async_call_router_chat', 'call_router_chat']
