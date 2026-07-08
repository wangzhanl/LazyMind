from __future__ import annotations

import asyncio
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
from tenacity import AsyncRetrying, retry_if_result, stop_after_attempt, wait_random_exponential

DEFAULT_DISABLED_TOOLS = tuple(
    'temp_kb calculator wikipedia web_search academic_search url_fetch multimodal image_generator image_editor '
    'vocab_learn read_memory memory_editor skill_editor local_fs feishu notion '
    'schedule create_schedule list_schedules cancel_schedule update_schedule trigger_schedule '
    'ask_user create_subagent list_subagents read_user_attachment find_user_attachment mcp plugin'.split()
)
EVO_EVAL_MEMORY = (
    'Evo evaluation request: use the knowledge-base tool as the evidence source, '
    'keep retrieval bounded, answer directly when enough evidence is available, '
    'and do not ask the user or schedule tasks.'
)

TRACE_ID = re.compile(r'^[0-9a-f]{32}$')
SSE_FIELD = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*:')
CONTROL_TAG = re.compile(r'<(?:tp|trp|tool_call|tool_result)(?:\s[^>]*)?>.*?</(?:tp|trp|tool_call|tool_result)>', re.S)
INVALID_FINAL_ANSWER_APOLOGY = re.compile(r'^(?:抱歉|很抱歉|对不起|sorry)[，,。.!:\s]*')
INVALID_FINAL_ANSWER_MARKERS = (
    '知识库检索服务暂时不可用',
    '知识库服务暂时不可用',
    '无法访问知识库',
    '无法检索知识库',
    '检索服务暂时不可用',
    'the knowledge base retrieval service is unavailable',
    'knowledge base retrieval service is unavailable',
    'kb retrieval service is unavailable',
    'retrieval service is unavailable',
)

DEFAULT_CASE_DEADLINE_SECONDS = 300.0
DEFAULT_FIRST_FRAME_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_WAIT_MAX_SECONDS = 2.0
ROUTER_ADMIN_TIMEOUT_SECONDS = 10.0
ROUTER_CANCEL_TIMEOUT_SECONDS = 2.0
STREAM_CLOSE_TIMEOUT_SECONDS = 2.0
INVALID_FINAL_ANSWER_MAX_CHARS = 180


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
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retry_wait_max_seconds: float = DEFAULT_RETRY_WAIT_MAX_SECONDS


@dataclass
class ChatStreamState:
    frames: list[Mapping[str, Any]]
    answer_parts: list[str]
    sources: list[dict[str, Any]]
    finished: bool = False

    @property
    def answer(self) -> str:
        return ''.join(self.answer_parts).strip()


def call_router_chat(request: RouterChatRequest) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(async_call_router_chat(request))
    return _failed({}, _target_from_raw(request), 'chat_runtime_error',
                   'call async_call_router_chat from a running event loop')


async def async_call_router_chat(request: RouterChatRequest) -> dict[str, Any]:
    try:
        normalized = _normalize_request(request)
    except (TypeError, ValueError) as exc:
        return _failed({}, _target_from_raw(request), 'chat_config_error', str(exc))

    attempts = 0
    deadline = time.monotonic() + normalized.case_deadline_seconds
    wait_strategy = wait_random_exponential(multiplier=0.25, max=normalized.retry_wait_max_seconds)

    async def attempt_once() -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _failed({}, _target(normalized), 'chat_timeout', 'chat retry budget exhausted before next attempt')
        attempt = normalized if attempts == 1 else _retry_request(normalized)
        return await _call_router_chat_once(replace(
            attempt,
            case_deadline_seconds=remaining,
            first_frame_timeout_seconds=min(attempt.first_frame_timeout_seconds, remaining),
        ))

    def wait_with_deadline(state: Any) -> float:
        return min(float(wait_strategy(state)), max(0.0, deadline - time.monotonic()))

    result = await AsyncRetrying(
        stop=stop_after_attempt(normalized.max_attempts),
        wait=wait_with_deadline,
        retry=retry_if_result(_retryable_chat_result),
        retry_error_callback=_retry_exhausted_result,
        reraise=False,
    )(attempt_once)
    if attempts > 1:
        result = dict(result) | {'chat_attempt_count': attempts}
    return result


async def _call_router_chat_once(request: RouterChatRequest) -> dict[str, Any]:
    deadline = time.monotonic() + request.case_deadline_seconds
    timeout = httpx.Timeout(
        connect=request.connect_timeout_seconds,
        write=request.write_timeout_seconds,
        read=None,
        pool=request.pool_timeout_seconds,
    )
    try:
        return await _stream_chat(request, timeout, deadline)
    except asyncio.TimeoutError:
        target = await _cancel_chat(request, _target(request))
        return _failed({}, target, 'chat_timeout', 'chat stream exceeded case deadline after 0 frame(s)')
    except httpx.HTTPError as exc:
        target = await _cancel_chat(request, _target(request))
        return _failed({}, target, 'chat_transport_error', str(exc))
    except Exception as exc:
        return _failed({}, _target(request), 'chat_unknown_error', f'{type(exc).__name__}: {exc}')


async def _stream_chat(
    request: RouterChatRequest,
    timeout: httpx.Timeout,
    deadline: float,
) -> dict[str, Any]:
    target = _target(request)
    state = ChatStreamState(frames=[], answer_parts=[], sources=[])
    stream_cm: Any | None = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        detail = await _router_algorithm_detail(client, request, deadline)
        if isinstance(detail.get('chat_error'), Mapping):
            return dict(detail)
        try:
            target = _target_with_router_detail(target, detail, request)
        except ValueError as exc:
            return _failed(state, target, 'router_algorithm_protocol_error', str(exc))
        first_frame_deadline = time.monotonic() + request.first_frame_timeout_seconds
        try:
            stream_cm = client.stream(
                'POST',
                str(target['routed_chat_url']),
                json=_payload(request),
                headers={'Accept': 'text/event-stream', 'Content-Type': 'application/json'},
            )
            response = await asyncio.wait_for(
                stream_cm.__aenter__(),
                timeout=_remaining_seconds(deadline, first_frame_deadline, False),
            )
            try:
                if response.status_code != 200:
                    return _failed(state, target, 'chat_http_error', await _http_error(response, deadline))
                return await _consume_response(client, request, target, state, response, deadline, first_frame_deadline)
            finally:
                await _close_response(response)
        except asyncio.TimeoutError:
            target = await _cancel_chat(request, target, client=client)
            return _timeout_failure(state, target)
        except httpx.HTTPError as exc:
            target = await _cancel_chat(request, target, client=client)
            return _failed(state, target, 'chat_transport_error', str(exc))
        finally:
            await _exit_stream(stream_cm)


async def _consume_response(
    client: httpx.AsyncClient,
    request: RouterChatRequest,
    target: Mapping[str, Any],
    state: ChatStreamState,
    response: httpx.Response,
    deadline: float,
    first_frame_deadline: float,
) -> dict[str, Any]:
    pending_data_lines: list[str] = []
    lines = response.aiter_lines()

    while True:
        remaining = _remaining_seconds(deadline, first_frame_deadline, bool(state.frames))
        if remaining <= 0:
            target = await _cancel_chat(request, target, client=client)
            return _timeout_failure(state, target)
        try:
            line = await asyncio.wait_for(lines.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            if pending_data_lines:
                result = _accept_payload(target, state, '\n'.join(pending_data_lines))
                if result is not None:
                    return result
            return _normalize(target, state)

        payload_text = _sse_payload(str(line or ''), pending_data_lines)
        if payload_text is None:
            continue
        result = _accept_payload(target, state, payload_text)
        if result is not None:
            return result


def _target_with_router_detail(
    target: Mapping[str, Any],
    detail: Mapping[str, Any],
    request: RouterChatRequest,
) -> dict[str, Any]:
    instance_urls = [str(url) for url in detail.get('healthy_instance_urls') or [] if str(url).strip()]
    if not instance_urls:
        raise ValueError('router detail did not include a healthy instance URL')
    routed_url = _stream_url(instance_urls[0])
    return dict(target) | dict(detail) | {
        'routed_algorithm_id': request.algorithm_id,
        'routed_instance_host': urlparse(routed_url).netloc,
        'routed_chat_url': routed_url,
    }


async def _router_algorithm_detail(
    client: httpx.AsyncClient,
    request: RouterChatRequest,
    deadline: float,
) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _failed({}, _target(request), 'router_algorithm_timeout',
                       'algorithm detail request exceeded case deadline')
    try:
        response = await asyncio.wait_for(
            client.get(_algorithm_url(request.router_admin_url, request.algorithm_id)),
            timeout=min(ROUTER_ADMIN_TIMEOUT_SECONDS, remaining),
        )
    except asyncio.TimeoutError:
        return _failed({}, _target(request), 'router_algorithm_timeout',
                       'algorithm detail request timed out')
    except httpx.HTTPError as exc:
        return _failed({}, _target(request), 'router_algorithm_transport_error', str(exc))
    if response.status_code != 200:
        return _failed({}, _target(request), 'router_algorithm_unavailable',
                       _http_status_message(response.status_code, response.text))
    try:
        detail = response.json()
    except json.JSONDecodeError:
        return _failed({}, _target(request), 'router_algorithm_protocol_error',
                       'algorithm detail is not JSON')
    if not isinstance(detail, Mapping):
        return _failed({}, _target(request), 'router_algorithm_protocol_error',
                       'algorithm detail is not an object')

    instances = detail.get('instances') if isinstance(detail.get('instances'), list) else []
    healthy = [item for item in instances if isinstance(item, Mapping) and item.get('status') == 'healthy']
    if detail.get('status') != 'active' or not healthy:
        return _failed({}, _target(request), 'router_algorithm_unhealthy',
                       f'algorithm {request.algorithm_id} is not active with healthy instances')
    return {
        'algorithm_status': str(detail.get('status') or ''),
        'healthy_instances': len(healthy),
        'healthy_instance_urls': _instance_urls(healthy),
    }


def _payload(request: RouterChatRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
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


def _accept_payload(
    target: Mapping[str, Any],
    state: ChatStreamState,
    payload_text: str,
) -> dict[str, Any] | None:
    text = payload_text.strip()
    try:
        frame = json.loads(text)
    except json.JSONDecodeError:
        return _failed(ChatStreamState([], [], []), target, 'chat_protocol_error', 'non-json SSE data')
    if not isinstance(frame, Mapping):
        return _failed(ChatStreamState([], [], []), target, 'chat_protocol_error', 'SSE JSON is not an object')

    missing = {'code', 'msg', 'data', 'cost'} - set(frame.keys())
    if missing:
        return _failed(state, target, 'chat_protocol_error',
                       'SSE JSON envelope missing ' + ','.join(sorted(missing)))
    if not isinstance(frame.get('data'), Mapping):
        return _failed(state, target, 'chat_protocol_error', 'SSE JSON envelope missing data object')
    data = frame['data']
    state.frames.append(frame)

    code = frame['code']
    status = str(data.get('status') or '').upper()
    if _code_failed(code) or status == 'FAILED':
        message = frame.get('msg') or data.get('message') or status or f'code={code}'
        return _failed(state, target, 'chat_business_error', str(message))

    _extend_sources(state.sources, data.get('sources'))
    visible_text = _visible_text(str(data.get('text') or ''))
    if visible_text:
        state.answer_parts.append(visible_text)

    if status == 'FINISHED':
        state.finished = True
        return _normalize(target, state)
    return None


def _normalize(target: Mapping[str, Any], stream: Mapping[str, Any] | ChatStreamState) -> dict[str, Any]:
    if isinstance(stream, ChatStreamState):
        finished = stream.finished
        answer = _answer_text(stream.answer)
        sources = _unique_sources(stream.sources)
    else:
        finished = bool(stream.get('finished'))
        frames = [frame for frame in stream.get('frames', []) if isinstance(frame, Mapping)]
        data_frames = [_frame_data(frame) for frame in frames]
        answer = _answer_text(str(
            stream.get('explicit_answer')
            or stream.get('answer')
            or ''.join(_visible_text(str(data.get('text') or '')) for data in data_frames)
        ))
        sources = _unique_sources(_sources(data_frames))

    if not finished:
        return _failed({'answer': ''}, target, 'chat_protocol_error', 'stream ended before FINISHED')
    if not answer:
        return _failed({'answer': ''}, target, 'chat_no_answer', 'stream finished without final answer text')
    if _invalid_final_answer(answer):
        return _failed({'answer': ''}, target, 'chat_invalid_answer',
                       'stream finished with retrieval-unavailable final answer')

    contexts, doc_ids, chunk_ids = _source_refs(sources, target)
    return {
        'status': 'ok',
        'answer': answer,
        'trace_id': str(target.get('trace_id') or ''),
        'algorithm_id': str(target.get('algorithm_id') or ''),
        'routed_algorithm_id': str(target.get('routed_algorithm_id') or ''),
        'routed_instance_host': str(target.get('routed_instance_host') or ''),
        'contexts': _unique(contexts),
        'doc_ids': _unique(doc_ids),
        'chunk_ids': _unique(chunk_ids),
        'sources': sources,
        'tool_errors': [],
        'frames': [],
        'chat_error': None,
        'target': dict(target),
    }


def _failed(
    stream: Mapping[str, Any] | ChatStreamState,
    target: Mapping[str, Any],
    error_type: str,
    message: str,
) -> dict[str, Any]:
    answer = stream.answer if isinstance(stream, ChatStreamState) else str(stream.get('answer') or '')
    return {
        'status': 'failed',
        'answer': answer,
        'trace_id': str(target.get('trace_id') or ''),
        'algorithm_id': str(target.get('algorithm_id') or ''),
        'routed_algorithm_id': str(target.get('routed_algorithm_id') or ''),
        'routed_instance_host': str(target.get('routed_instance_host') or ''),
        'contexts': [],
        'doc_ids': [],
        'chunk_ids': [],
        'sources': [],
        'tool_errors': [],
        'frames': [],
        'chat_error': {'type': error_type, 'message': message},
        'target': dict(target),
    }


async def _cancel_chat(
    request: RouterChatRequest,
    target: Mapping[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    urls = _cancel_urls(request.router_admin_url, target.get('healthy_instance_urls'))
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=ROUTER_CANCEL_TIMEOUT_SECONDS)
    try:
        last_error = ''
        for url in urls:
            try:
                response = await active_client.post(
                    url,
                    json={'conversation_id': request.conversation_id},
                    timeout=ROUTER_CANCEL_TIMEOUT_SECONDS,
                )
                if response.status_code == 200 and bool(response.json().get('ok')):
                    return dict(target) | {'chat_cancel_requested': True, 'chat_cancel_ok': True}
                last_error = f'HTTP {response.status_code}'
            except Exception as exc:
                last_error = f'{type(exc).__name__}: {exc}'
        return dict(target) | {
            'chat_cancel_requested': True,
            'chat_cancel_ok': False,
            'chat_cancel_error': last_error,
        }
    finally:
        if owns_client:
            await active_client.aclose()


def _normalize_request(request: RouterChatRequest) -> RouterChatRequest:
    router_chat_url = _stream_url(request.router_chat_url)
    router_admin_url = _base_url(request.router_admin_url, 'router_admin_url')
    algorithm_id = str(request.algorithm_id or '').strip()
    query = str(request.query or '').strip()
    kb_ids = tuple(dict.fromkeys(str(item).strip() for item in request.kb_ids if str(item).strip()))
    trace_id = str(request.trace_id or uuid4().hex).strip().lower()
    conversation_id = str(request.conversation_id or trace_id).strip()
    disabled_tools = tuple(dict.fromkeys((
        *DEFAULT_DISABLED_TOOLS,
        *(str(item).strip() for item in (request.disabled_tools or ()) if str(item).strip()),
    )))

    if not algorithm_id:
        raise ValueError('algorithm_id is required')
    if not query:
        raise ValueError('query is required')
    if not kb_ids:
        raise ValueError('kb_ids is required')
    if not TRACE_ID.fullmatch(trace_id):
        raise ValueError('trace_id must be a 32-character lowercase hex string')

    return replace(
        request,
        router_chat_url=router_chat_url,
        router_admin_url=router_admin_url,
        algorithm_id=algorithm_id,
        query=query,
        kb_ids=kb_ids,
        trace_id=trace_id,
        conversation_id=conversation_id,
        user_id=str(request.user_id or '0').strip() or '0',
        history=tuple(dict(item) for item in request.history),
        disabled_tools=disabled_tools,
        connect_timeout_seconds=_positive_number(request.connect_timeout_seconds, 'connect_timeout_seconds'),
        write_timeout_seconds=_positive_number(request.write_timeout_seconds, 'write_timeout_seconds'),
        pool_timeout_seconds=_positive_number(request.pool_timeout_seconds, 'pool_timeout_seconds'),
        case_deadline_seconds=_positive_number(request.case_deadline_seconds, 'case_deadline_seconds'),
        first_frame_timeout_seconds=_positive_number(
            request.first_frame_timeout_seconds,
            'first_frame_timeout_seconds',
        ),
        max_attempts=_int_between(request.max_attempts, 'max_attempts', 1, 5),
        retry_wait_max_seconds=_non_negative_number(request.retry_wait_max_seconds, 'retry_wait_max_seconds'),
    )


def _retry_request(request: RouterChatRequest) -> RouterChatRequest:
    trace_id = uuid4().hex
    return replace(request, trace_id=trace_id, conversation_id=trace_id)


def _retryable_chat_result(result: Mapping[str, Any]) -> bool:
    error = result.get('chat_error') if isinstance(result.get('chat_error'), Mapping) else {}
    error_type = str(error.get('type') or '')
    if error_type in {
        'chat_no_answer',
        'chat_invalid_answer',
        'chat_transport_error',
        'router_algorithm_timeout',
        'router_algorithm_transport_error',
        'router_algorithm_unavailable',
        'router_algorithm_unhealthy',
    }:
        return True
    if error_type == 'chat_http_error':
        return any(code in str(error.get('message') or '') for code in ('HTTP 429', 'HTTP 502', 'HTTP 503', 'HTTP 504'))
    if error_type == 'chat_timeout':
        return 'first-frame deadline' in str(error.get('message') or '')
    return False


def _retry_exhausted_result(state: Any) -> dict[str, Any]:
    result = state.outcome.result()
    if not _retryable_chat_result(result):
        return dict(result)
    target = result.get('target') if isinstance(result.get('target'), Mapping) else {}
    error = result.get('chat_error') if isinstance(result.get('chat_error'), Mapping) else {}
    exhausted_type = f'{str(error.get("type") or "").strip()}_retry_exhausted'
    messages = {
        'chat_invalid_answer_retry_exhausted': 'chat stream repeatedly returned retrieval-unavailable final answer',
        'chat_no_answer_retry_exhausted': 'chat stream repeatedly finished without final answer text',
        'chat_transport_error_retry_exhausted': 'chat stream repeatedly failed with transport errors',
        'chat_http_error_retry_exhausted': 'chat stream repeatedly failed with retryable HTTP errors',
        'chat_timeout_retry_exhausted': 'chat stream repeatedly exceeded the first-frame deadline',
        'router_algorithm_unhealthy_retry_exhausted': 'router repeatedly reported no healthy algorithm instance',
        'router_algorithm_timeout_retry_exhausted': 'router algorithm detail repeatedly timed out',
        'router_algorithm_transport_error_retry_exhausted': 'router algorithm detail repeatedly failed with transport errors',
        'router_algorithm_unavailable_retry_exhausted': 'router algorithm detail repeatedly reported unavailable algorithm',
    }
    return _failed({}, target, exhausted_type, messages.get(exhausted_type, 'chat stream repeatedly returned retryable failure')) | {
        'retry_exhausted': True,
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


def _frame_data(frame: Mapping[str, Any]) -> Mapping[str, Any]:
    data = frame.get('data')
    return data if isinstance(data, Mapping) else {}


def _visible_text(text: str) -> str:
    return re.sub(r'\n{3,}', '\n\n', CONTROL_TAG.sub('', str(text or ''))).strip()


def _answer_text(raw_answer: str) -> str:
    return _visible_text(raw_answer)


def _sources(frames: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for data in frames:
        _extend_sources(sources, data.get('sources'))
    return sources


def _extend_sources(target: list[dict[str, Any]], value: Any) -> None:
    if isinstance(value, list):
        target.extend(dict(item) for item in value if isinstance(item, Mapping))


def _unique_sources(sources: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for source in sources:
        key = str(
            source.get('index')
            or source.get('segement_id')
            or source.get('segment_id')
            or source.get('chunk_id')
            or source.get('document_id')
            or source.get('doc_id')
            or id(source)
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(source))
    return result


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
        metadata = source.get('global_metadata') if isinstance(source.get('global_metadata'), Mapping) else {}
        kb_id = str(source.get('kb_id') or source.get('dataset_id') or metadata.get('kb_id')
                    or metadata.get('dataset_id') or fallback_kb).strip()
        doc = str(source.get('doc_id') or source.get('docid') or source.get('document_id')
                  or metadata.get('docid') or metadata.get('core_document_id') or '').strip()
        chunk = str(source.get('chunk_id') or source.get('chunkid') or source.get('segment_id')
                    or source.get('segement_id') or source.get('uid') or source.get('id') or '').strip()
        doc_ref = doc if ':' in doc else f'{kb_id}:{doc}' if kb_id and doc else doc
        chunk_ref = chunk if ':' in chunk else f'{doc_ref}:{chunk}' if doc_ref and chunk else chunk
        contexts.append(source.get('content') or source.get('text') or source.get('chunk'))
        doc_ids.append(doc_ref)
        chunk_ids.append(chunk_ref)
    return contexts, doc_ids, chunk_ids


def _invalid_final_answer(answer: str) -> bool:
    text = re.sub(r'\s+', ' ', str(answer or '').strip()).lower()
    if len(text) > INVALID_FINAL_ANSWER_MAX_CHARS:
        return False
    if not any(marker in text for marker in INVALID_FINAL_ANSWER_MARKERS):
        return False
    text = text.lstrip(' "\'“”‘’')
    if text.startswith(INVALID_FINAL_ANSWER_MARKERS):
        return True
    match = INVALID_FINAL_ANSWER_APOLOGY.match(text)
    return bool(match and text[match.end():].lstrip(' "\'“”‘’').startswith(INVALID_FINAL_ANSWER_MARKERS))


def _instance_urls(instances: Sequence[Mapping[str, Any]]) -> list[str]:
    urls: list[str] = []
    for item in instances:
        host = str(item.get('host') or '').strip()
        try:
            port = int(item.get('port') or 0)
        except (TypeError, ValueError):
            port = 0
        if host and port > 0:
            urls.append(f'http://{host}:{port}')
    return urls


def _cancel_urls(router_admin_url: str, instance_urls: Any = None) -> list[str]:
    urls: list[str] = []
    for url in instance_urls or []:
        try:
            urls.append(_task_cancel_url(url))
        except ValueError:
            pass
    urls.append(_task_cancel_url(router_admin_url))
    return list(dict.fromkeys(urls))


async def _http_error(response: httpx.Response, deadline: float) -> str:
    try:
        body = await asyncio.wait_for(response.aread(), timeout=max(0.001, deadline - time.monotonic()))
    except asyncio.TimeoutError:
        return f'HTTP {response.status_code}: <error body read timed out>'
    return _http_status_message(response.status_code, body.decode(errors='replace'))


def _http_status_message(status_code: int, body: Any) -> str:
    text = str(body or '').strip()
    return f'HTTP {status_code}' + (f': {text[:300]}' if text else '')


def _timeout_failure(stream: Mapping[str, Any] | ChatStreamState, target: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(stream, ChatStreamState):
        count = len(stream.frames)
        last = _frame_data(stream.frames[-1]) if stream.frames else {}
    else:
        frames = [frame for frame in stream.get('frames', []) if isinstance(frame, Mapping)]
        count = len(frames)
        last = _frame_data(frames[-1]) if frames else {}
    if not count:
        return _failed(stream, target, 'chat_timeout', 'chat stream exceeded first-frame deadline after 0 frame(s)')
    parts = [f'chat stream exceeded case deadline after {count} frame(s)']
    if last.get('status'):
        parts.append(f'last_status={last["status"]}')
    if last.get('code') is not None:
        parts.append(f'last_code={last["code"]}')
    return _failed({}, target, 'chat_timeout', '; '.join(parts))


async def _close_response(response: httpx.Response) -> None:
    try:
        await asyncio.wait_for(response.aclose(), timeout=STREAM_CLOSE_TIMEOUT_SECONDS)
    except Exception:
        pass


async def _exit_stream(stream_cm: Any) -> None:
    if stream_cm is None:
        return
    try:
        await asyncio.wait_for(stream_cm.__aexit__(None, None, None), timeout=STREAM_CLOSE_TIMEOUT_SECONDS)
    except Exception:
        pass


def _remaining_seconds(deadline: float, first_frame_deadline: float, has_frames: bool) -> float:
    now = time.monotonic()
    remaining = deadline - now
    if not has_frames:
        remaining = min(remaining, first_frame_deadline - now)
    return max(0.0, remaining)


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
    return urlunparse((parsed.scheme, parsed.netloc, f'/inner/algorithm/{quote(algorithm_id, safe="")}', '', '', ''))


def _task_cancel_url(base_url: str) -> str:
    parsed = _parsed_http_url(base_url, 'task_cancel_url')
    return urlunparse((parsed.scheme, parsed.netloc, '/api/plugin/task-cancel', '', '', ''))


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


def _non_negative_number(value: Any, field: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f'{field} must be a non-negative finite number')
    return number


def _int_between(value: Any, field: str, low: int, high: int) -> int:
    number = int(value)
    if number < low or number > high:
        raise ValueError(f'{field} must be between {low} and {high}')
    return number


def _code_failed(value: Any) -> bool:
    return str(value).strip() != '200'


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value or '').strip()))


__all__ = ['RouterChatRequest', 'async_call_router_chat', 'call_router_chat']
