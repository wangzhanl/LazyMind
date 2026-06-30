from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, NamedTuple

PERMISSIONS = {
    **dict.fromkeys(('read', 'grep', 'glob', 'list', 'edit', 'write'), 'allow'),
    **dict.fromkeys(('bash', 'question', 'plan_enter', 'plan_exit', 'todowrite', 'task'), 'deny'),
}
OPENCODE_CONFIG_KEYS = {
    'OPENCODE_MODEL',
    'OPENCODE_PROVIDER',
    'OPENCODE_PROVIDER_MODEL',
    'OPENCODE_PROVIDER_LABEL',
    'OPENCODE_PROVIDER_BASE_URL',
    'OPENCODE_PROVIDER_KEY_ENV',
}


class OpenCodeRunResult(NamedTuple):
    returncode: int
    session_id: str
    events: list[dict[str, Any]]
    raw_paths: dict[str, str]
    prompt_arg: str
    last_error: dict[str, Any] | None
    duration_seconds: float
    setup_seconds: float
    first_response_seconds: float | None
    model: str
    provider: str


def run_opencode_streaming(
    *,
    workdir: str,
    prompt: str,
    artifact_dir: Path,
    session_id: str = '',
    env: dict[str, str] | None = None,
    timeout_s: int = 900,
    first_response_timeout_s: int = 300,
) -> OpenCodeRunResult:
    started = time.time()
    stdout: list[str] = []
    events: list[dict[str, Any]] = []
    safe_env, secrets = _opencode_env(env or {}), _secrets(env or {})

    def fail(kind: str, message: object, prompt_arg: str = '', setup_done: float | None = None) -> OpenCodeRunResult:
        error = _clean({'type': kind, 'message': str(message)}, secrets)
        events.append(error)
        paths = _write_logs(artifact_dir, stdout, events, secrets)
        return _result(1, session_id, events, paths, prompt_arg, error, started, setup_done or time.time(), None,
                       safe_env)

    if missing := _missing_config(safe_env):
        return fail('configuration_error', f'missing opencode config fields: {", ".join(missing)}')
    try:
        root = Path(workdir).resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = artifact_dir / 'opencode_prompt.json'
        config_path = root / 'opencode.json'
        prompt_path.write_text(prompt, encoding='utf-8')
        config_path.write_text(json.dumps(_opencode_config(safe_env), ensure_ascii=False), encoding='utf-8')
    except Exception as exc:
        return fail('prompt_write_failed', exc)

    prompt_arg = f'Read {prompt_path.as_posix()} first, then follow the JSON task card exactly.'
    events.append({'type': 'setup', 'status': 'completed', 'message': f'workdir={root}'})
    setup_done = time.time()
    events.append({'type': 'process_start', 'status': 'running', 'message': 'starting opencode'})
    try:
        proc = subprocess.Popen(
            _cmd(prompt_arg, session_id, safe_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(root),
            env=_process_env(safe_env),
            start_new_session=True,
        )
    except Exception as exc:
        return fail('process_start_failed', exc, prompt_arg, setup_done)

    session, error, first, heartbeat = session_id, None, None, setup_done
    while proc.poll() is None:
        now = time.time()
        if now - started > timeout_s:
            error = {'type': 'timeout', 'message': f'opencode timed out after {timeout_s}s'}
            _terminate(proc)
            break
        ready, _, _ = select.select([proc.stdout], [], [], 0.05) if proc.stdout else ([], [], [])
        if not ready:
            if first is None and now - started >= first_response_timeout_s:
                error = {
                    'type': 'first_response_timeout',
                    'message': f'opencode produced no model/tool event within {first_response_timeout_s}s',
                }
                _terminate(proc)
                break
            if now - heartbeat >= 10:
                heartbeat = now
                events.append({'type': 'process_heartbeat', 'status': 'running',
                               'elapsed_seconds': round(now - started, 1), 'changed_files': _changed(root)})
            continue
        session, error, first = _read_line(ready[0].readline(), stdout, events, session, error, first,
                                           started, secrets)
    if proc.stdout:
        for line in proc.stdout:
            session, error, first = _read_line(line, stdout, events, session, error, first, started, secrets)
    returncode = proc.wait()
    events.append({'type': 'process_exit', 'status': 'completed' if returncode == 0 else 'failed',
                   'message': f'opencode exited with code {returncode}'})
    if returncode and not error:
        error = {'type': 'process_failed', 'message': _clean(''.join(stdout)[-1000:], secrets)}
        events.append(error)
    paths = _write_logs(artifact_dir, stdout, events, secrets)
    return _result(returncode, session, events, paths, prompt_arg, error, started, setup_done, first, safe_env)


def trace_payload(result: OpenCodeRunResult, repair_plan_ref: str, attempt: int) -> dict[str, Any]:
    compact = [_compact(i, event) for i, event in enumerate(result.events)]
    projected = [_project(item) for item in compact]
    projected = [item for item in projected if item]
    files = sorted({
        path for event in compact
        if event.get('tool') in {'edit', 'write'} or event.get('event_type') in {'patch', 'code_patch'}
        for path in event.get('file_paths', [])
    })
    if files:
        projected.append({
            'event_id': f'opencode_{attempt}_patch',
            'phase': 'opencode_patch',
            'source': 'opencode',
            'kind': 'code_patch',
            'status': 'completed',
            'severity': 'info',
            'title': 'Code patch',
            'summary': f'{len(files)} file(s) changed',
            'paths': files,
        })
    return {
        'id': f'opencode_run_trace_attempt_{attempt}',
        'repair_plan_ref': repair_plan_ref,
        'attempt': attempt,
        'returncode': result.returncode,
        'raw_paths': result.raw_paths,
        'prompt_delivery': {'mode': 'file', 'instruction': result.prompt_arg,
                            'prompt_path': result.raw_paths.get('prompt', '')},
        'provider': result.provider,
        'model': result.model,
        'session_mapping': {'status': 'mapped' if result.session_id else 'unmapped',
                            'source': 'opencode_stdout_json_events', 'session_id': result.session_id},
        'event_counts': {kind: sum(str(e.get('type') or 'unknown') == kind for e in result.events)
                         for kind in sorted({str(e.get('type') or 'unknown') for e in result.events})},
        'compact_events': compact,
        'projected_events': projected,
        'files_modified': files,
        'last_error': result.last_error,
        'duration_seconds': result.duration_seconds,
        'setup_seconds': result.setup_seconds,
        'first_response_seconds': result.first_response_seconds,
    }


def _result(returncode: int, session: str, events: list[dict[str, Any]], paths: dict[str, str], prompt_arg: str,
            error: dict[str, Any] | None, started: float, setup_done: float, first: float | None,
            env: dict[str, str]) -> OpenCodeRunResult:
    return OpenCodeRunResult(
        returncode=returncode,
        session_id=session,
        events=events,
        raw_paths=paths,
        prompt_arg=prompt_arg,
        last_error=error,
        duration_seconds=round(time.time() - started, 3),
        setup_seconds=round(setup_done - started, 3),
        first_response_seconds=first,
        model=env.get('OPENCODE_MODEL', ''),
        provider=env.get('OPENCODE_PROVIDER', ''),
    )


def _read_line(line: str, stdout: list[str], events: list[dict[str, Any]], session: str,
               error: dict[str, Any] | None, first: float | None, start: float,
               secrets: list[str]) -> tuple[str, dict[str, Any] | None, float | None]:
    if not line:
        return session, error, first
    stdout.append(_clean(line, secrets))
    try:
        event = _clean(json.loads(line), secrets)
    except json.JSONDecodeError:
        text = _clean(line.strip(), secrets)
        if text:
            events.append({'type': 'stdout', 'status': 'running', 'message': str(text)[:300]})
        return session, error, first
    if isinstance(event, dict):
        events.append(event)
        if first is None and (_tool(event) or _message(event) or event.get('type') == 'error'):
            first = round(time.time() - start, 3)
        return session or str(event.get('sessionID') or ''), event if event.get('type') == 'error' else error, first
    return session, error, first


def _cmd(prompt: str, session: str, env: dict[str, str]) -> list[str]:
    binary = os.getenv('LAZYMIND_EVO_CODE_BINARY') or 'opencode'
    args = [binary, 'run', '--format', 'json']
    if env.get('OPENCODE_MODEL'):
        args += ['--model', env['OPENCODE_MODEL']]
    if session:
        args += ['--session', session]
    return [*args, prompt]


def _opencode_config(env: dict[str, str]) -> dict[str, Any]:
    provider, model = env.get('OPENCODE_PROVIDER', ''), env.get('OPENCODE_PROVIDER_MODEL', '')
    base_url, key_env = env.get('OPENCODE_PROVIDER_BASE_URL', ''), env.get('OPENCODE_PROVIDER_KEY_ENV', '')
    config: dict[str, Any] = {'$schema': 'https://opencode.ai/config.json', 'permission': PERMISSIONS}
    if provider and model and base_url and key_env and env.get(key_env):
        official = base_url.rstrip('/').endswith('api.openai.com/v1')
        npm = '@ai-sdk/openai' if provider == 'openai' and official else '@ai-sdk/openai-compatible'
        model_cfg: dict[str, Any] = {'name': model, 'tool_call': True}
        if not official:
            model_cfg['limit'] = {'context': 32768, 'output': 1024}
        config['provider'] = {provider: {
            'npm': npm,
            'name': env.get('OPENCODE_PROVIDER_LABEL') or provider,
            'options': {'baseURL': base_url, 'apiKey': f'{{env:{key_env}}}'},
            'models': {model: model_cfg},
        }}
    return config


def _compact(index: int, event: dict[str, Any]) -> dict[str, Any]:
    paths = _paths(event)
    for key in ('changed_files', 'files'):
        paths += [str(path) for path in event.get(key, []) if isinstance(path, str)]
    return {
        'index': index,
        'event_type': str(event.get('type') or 'unknown'),
        'tool': _tool(event),
        'execution_type': _execution_type(event),
        'summary': _message(event)[:500],
        'file_paths': sorted(set(paths)),
        'command': _command(event),
        'status': str(event.get('status') or event.get('state') or ''),
    }


def _project(event: dict[str, Any]) -> dict[str, Any] | None:
    tool = str(event.get('tool') or '')
    kind = {
        'glob': 'tool_use.search',
        'grep': 'tool_use.search',
        'list': 'tool_use.search',
        'read': 'tool_use.read_file',
        'edit': 'tool_use.edit_file',
        'write': 'tool_use.edit_file',
        'bash': 'tool_use.run_command',
    }.get(tool) or {
        'setup': 'setup',
        'process_start': 'process',
        'process_exit': 'process',
        'sync_back': 'sync',
        'process_heartbeat': 'heartbeat',
        'stdout': 'agent_note',
        'error': 'error',
        'timeout': 'error',
        'first_response_timeout': 'error',
        'process_failed': 'error',
    }.get(str(event.get('event_type') or ''))
    if not kind and event.get('summary'):
        kind = 'agent_note'
    if not kind:
        return None
    return {
        'event_id': f'opencode_raw_{event["index"]}',
        'phase': 'opencode_patch',
        'source': 'opencode',
        'kind': kind,
        'status': 'completed' if event.get('status') == 'completed' else event.get('status') or 'running',
        'severity': 'error' if kind == 'error' else 'info',
        'title': kind.replace('.', ' ').replace('_', ' '),
        'summary': event.get('summary') or event.get('command') or '',
        'paths': event.get('file_paths') or [],
        'raw_event_ref': event['index'],
        'tool': tool,
        'execution_type': event.get('execution_type'),
        'command': event.get('command'),
    }


def _execution_type(event: dict[str, Any]) -> str:
    if _tool(event):
        return 'tool_use'
    if event.get('type') in {'text', 'stdout'}:
        return 'code' if 'diff --git' in _message(event) else 'message'
    return str(event.get('type') or 'unknown')


def _tool(event: dict[str, Any]) -> str:
    part = event.get('part') if isinstance(event.get('part'), dict) else {}
    call = event.get('call') if isinstance(event.get('call'), dict) else {}
    return str(event.get('tool') or part.get('tool') or call.get('tool') or '')


def _message(event: dict[str, Any]) -> str:
    part = event.get('part') if isinstance(event.get('part'), dict) else {}
    text = part.get('text') or event.get('text') or event.get('message') or event.get('error') or ''
    return str(text).strip()


def _command(event: dict[str, Any]) -> str:
    for key in ('command', 'cmd'):
        if event.get(key):
            return str(event[key])
    return ''


def _paths(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [item for key, child in value.items()
                for item in ([child] if key in {'file', 'path', 'filepath', 'filePath'} and isinstance(child, str)
                             else _paths(child))]
    if isinstance(value, list):
        return [item for child in value for item in _paths(child)]
    return []


def _changed(workdir: Path) -> list[str]:
    try:
        result = subprocess.run(['git', '-C', str(workdir), 'diff', '--name-only'],
                                capture_output=True, text=True, timeout=5, check=False)
        return result.stdout.splitlines()
    except Exception:
        return []


def _opencode_env(raw: dict[str, str]) -> dict[str, str]:
    key_env = str(raw.get('OPENCODE_PROVIDER_KEY_ENV') or '').strip()
    allowed = OPENCODE_CONFIG_KEYS | ({key_env} if key_env else set())
    return {key: str(value).strip() for key, value in raw.items() if key in allowed and str(value).strip()}


def _missing_config(env: dict[str, str]) -> list[str]:
    required = ['OPENCODE_MODEL', 'OPENCODE_PROVIDER', 'OPENCODE_PROVIDER_MODEL',
                'OPENCODE_PROVIDER_BASE_URL', 'OPENCODE_PROVIDER_KEY_ENV']
    missing = [key for key in required if not env.get(key)]
    key_env = env.get('OPENCODE_PROVIDER_KEY_ENV', '')
    if key_env and not env.get(key_env):
        missing.append(key_env)
    return missing


def _process_env(env: dict[str, str]) -> dict[str, str]:
    base = {key: value for key in ('HOME', 'PATH', 'SHELL', 'USER', 'LANG', 'LC_ALL', 'TMPDIR')
            if (value := os.environ.get(key))}
    return {**base, **env}


def _write_logs(root: Path, stdout: list[str], events: list[dict[str, Any]], secrets: list[str]) -> dict[str, str]:
    try:
        root.mkdir(parents=True, exist_ok=True)
        paths = {'prompt': root / 'opencode_prompt.json', 'stdout': root / 'stdout.log',
                 'events_jsonl': root / 'events.jsonl'}
        paths['stdout'].write_text(''.join(stdout), encoding='utf-8')
        paths['events_jsonl'].write_text(
            ''.join(json.dumps(_clean(event, secrets), ensure_ascii=False) + '\n' for event in events),
            encoding='utf-8',
        )
        return {key: str(path) for key, path in paths.items()}
    except Exception:
        return {'prompt': '', 'stdout': '', 'events_jsonl': ''}


def _terminate(proc: subprocess.Popen, grace_s: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    for sig, stop in ((signal.SIGTERM, proc.terminate), (signal.SIGKILL, proc.kill)):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except Exception:
            stop()
        try:
            proc.wait(timeout=grace_s)
            return
        except subprocess.TimeoutExpired:
            pass


def _clean(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, '<redacted>')
        return value
    if isinstance(value, list):
        return [_clean(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item, secrets) for key, item in value.items()}
    return value


def _secrets(env: dict[str, str]) -> list[str]:
    return [str(value) for key, value in env.items() if value and any(x in key for x in ('KEY', 'TOKEN', 'SECRET'))]
