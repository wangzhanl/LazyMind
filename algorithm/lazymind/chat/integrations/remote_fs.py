import base64
import os
import shutil
from io import BytesIO, TextIOWrapper
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import lazyllm
import requests

from lazyllm.tools.fs import LazyLLMFSBase
from lazymind.config import config as _cfg


class RemoteFS(LazyLLMFSBase):
    """A filesystem proxy for skill resources using the backend core remote-fs API."""

    def __init__(self, token: str = 'remote', base_url: str = '', timeout: float = 10, **kwargs):
        super().__init__(token=token, **kwargs)
        self.base_url = (base_url or str(_cfg['core_api_url'] or '').strip()).rstrip('/')
        self.timeout = timeout
        self.local_root = str(_cfg['remote_fs_local_root'] or '').strip()

    @staticmethod
    def _normalize_path(path: str) -> str:
        raw = str(path or '')
        if '://' in raw:
            parsed = urlparse(raw)
            raw = f'{parsed.netloc}{parsed.path}'
        return raw.strip('/')

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        response = self._raw_request(method, endpoint, **kwargs)
        self._raise_for_status(response, endpoint)
        payload = response.json()
        if payload.get('code') != 0:
            raise RuntimeError(payload.get('message') or f'remote-fs {endpoint} failed')
        return payload.get('data')

    def _raw_request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        agentic_config = lazyllm.globals.get('agentic_config') or {}
        user_id = agentic_config.get('user_id')
        task_id = agentic_config.get('session_id')
        params = kwargs.pop('params', {})
        if user_id:
            params['user_id'] = user_id
        if task_id:
            params['task_id'] = task_id
        return requests.request(
            method,
            f'{self.base_url}/remote-fs/{endpoint}',
            params=params,
            timeout=self.timeout,
            **kwargs,
        )

    @staticmethod
    def _raise_for_status(response: requests.Response, endpoint: str) -> None:
        try:
            response.raise_for_status()
            return
        except requests.HTTPError as exc:
            message = RemoteFS._error_message(response)
            if message:
                raise RuntimeError(message) from exc
            raise

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return (response.text or '').strip()
        if isinstance(payload, dict):
            return str(payload.get('message') or payload.get('error') or '').strip()
        return ''

    def _request_json(self, endpoint: str, **params: Any) -> Any:
        return self._request('GET', endpoint, params=params)

    @staticmethod
    def _qualify_name(name: str) -> str:
        normalized = RemoteFS._normalize_path(name)
        return f'remote://{normalized}' if normalized else 'remote://'

    def _use_local_backend(self) -> bool:
        return bool(self.local_root)

    def _local_root_abs(self) -> str:
        return os.path.abspath(os.path.expanduser(self.local_root))

    def _local_path(self, path: str) -> str:
        normalized = self._normalize_path(path)
        root = self._local_root_abs()
        target = os.path.abspath(os.path.join(root, *normalized.split('/'))) if normalized else root
        if os.path.commonpath([root, target]) != root:
            raise RuntimeError(f'remote-fs local path escapes root: {path!r}')
        return target

    def _remote_name_from_local_path(self, local_path: str) -> str:
        rel = os.path.relpath(local_path, self._local_root_abs())
        if rel == '.':
            return ''
        return rel.replace(os.sep, '/')

    def _local_entry(self, local_path: str) -> Dict[str, Any]:
        stat = os.stat(local_path)
        return {
            'name': self._qualify_name(self._remote_name_from_local_path(local_path)),
            'size': stat.st_size,
            'type': 'directory' if os.path.isdir(local_path) else 'file',
            'mtime': str(stat.st_mtime),
        }

    def _format_list_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        formatted = dict(entry or {})
        formatted['name'] = self._qualify_name(str(formatted.get('name') or ''))
        return formatted

    def ls(self, path: str, detail: bool = True, **kwargs) -> List[Any]:
        normalized_path = self._normalize_path(path)
        if self._use_local_backend():
            local_path = self._local_path(normalized_path)
            if not os.path.exists(local_path):
                if normalized_path == 'skills':
                    return []
                raise FileNotFoundError(f'path does not exist: {self._qualify_name(normalized_path)}')
            if not os.path.isdir(local_path):
                entry = self._local_entry(local_path)
                return [entry] if detail else [entry['name']]
            entries = [
                self._local_entry(os.path.join(local_path, name))
                for name in sorted(os.listdir(local_path))
            ]
            return entries if detail else [entry['name'] for entry in entries]
        try:
            data = self._request_json('list', path=normalized_path, detail=str(bool(detail)).lower())
        except RuntimeError as exc:
            if normalized_path == 'skills' and 'path does not exist' in str(exc):
                return []
            raise
        if detail:
            return [self._format_list_entry(entry) for entry in data.get('entries', [])]
        return [self._qualify_name(name) for name in data.get('names', [])]

    def info(self, path: str, **kwargs) -> Dict[str, Any]:
        if self._use_local_backend():
            local_path = self._local_path(path)
            if not os.path.exists(local_path):
                raise FileNotFoundError(f'path does not exist: {self._qualify_name(path)}')
            return self._local_entry(local_path)
        return self._request_json('info', path=self._normalize_path(path))

    def exists(self, path: str, **kwargs) -> bool:
        if self._use_local_backend():
            return os.path.exists(self._local_path(path))
        data = self._request_json('exists', path=self._normalize_path(path))
        return bool(data.get('exists'))

    def makedirs(self, path: str, exist_ok: bool = True) -> None:
        return

    def mkdir(self, path: str, create_parents: bool = True, **kwargs) -> None:
        return

    def rm(self, path: str, recursive: bool = False, maxdepth: Optional[int] = None) -> None:
        if self._use_local_backend():
            local_path = self._local_path(path)
            if not os.path.exists(local_path):
                raise FileNotFoundError(f'path does not exist: {self._qualify_name(path)}')
            if os.path.isdir(local_path):
                if not recursive:
                    os.rmdir(local_path)
                else:
                    shutil.rmtree(local_path)
                return
            os.remove(local_path)
            return
        self._request(
            'DELETE',
            'path',
            params={
                'path': self._normalize_path(path),
                'recursive': str(bool(recursive)).lower(),
            },
        )

    def write(self, path: str, content: str, content_type: str = 'text/plain; charset=utf-8') -> None:
        if self._use_local_backend():
            local_path = self._local_path(path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, 'w', encoding='utf-8') as fh:
                fh.write(content)
            return
        self._request(
            'PUT',
            'content',
            params={'path': self._normalize_path(path)},
            data=content.encode('utf-8'),
            headers={'Content-Type': content_type},
        )

    def write_file(self, path: str, data: bytes, content_type: str = 'application/octet-stream') -> None:
        if self._use_local_backend():
            local_path = self._local_path(path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, 'wb') as fh:
                fh.write(data)
            return
        self._request(
            'PUT',
            'content',
            params={'path': self._normalize_path(path)},
            data=data,
            headers={'Content-Type': content_type},
        )

    def move(self, path1: str, path2: str, recursive: bool = False, **kwargs) -> None:
        if self._use_local_backend():
            source = self._local_path(path1)
            target = self._local_path(path2)
            if not os.path.exists(source):
                raise FileNotFoundError(f'path does not exist: {self._qualify_name(path1)}')
            if os.path.exists(target):
                raise FileExistsError(f'target already exists: {self._qualify_name(path2)}')
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(source, target)
            return
        self._request(
            'POST',
            'move',
            json={
                'from': self._normalize_path(path1),
                'to': self._normalize_path(path2),
            },
        )

    def open(
        self,
        path: str,
        mode: str = 'rb',
        block_size: Optional[int] = None,
        cache_options=None,
        compression=None,
        **kwargs,
    ):
        if self._use_local_backend():
            local_path = self._local_path(path)
            if any(flag in mode for flag in ('w', 'a', 'x', '+')):
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
            return open(local_path, mode, **kwargs)
        return self._open(path, mode=mode, block_size=block_size, **kwargs)

    def _open(
        self,
        path: str,
        mode: str = 'rb',
        block_size: Optional[int] = None,
        **kwargs,
    ):
        is_write = any(flag in mode for flag in ('w', 'a', 'x', '+'))
        if is_write:
            encoding = kwargs.get('encoding') or 'utf-8'
            content_type = kwargs.get('content_type')
            if not content_type and 'b' not in mode:
                content_type = f'text/plain; charset={encoding}'
            buf = BytesIO()

            class _WriteBuffer:
                def __init__(self, _buf, _path, _encoding, _content_type, _fs):
                    self._buf = _buf
                    self._path = _path
                    self._encoding = _encoding
                    self._content_type = _content_type
                    self._fs = _fs

                def write(self, data):
                    original_len = len(data)
                    if isinstance(data, str):
                        data = data.encode(self._encoding)
                    self._buf.write(data)
                    return original_len

                def close(self):
                    self._fs.write_file(
                        self._path,
                        self._buf.getvalue(),
                        content_type=self._content_type or 'application/octet-stream',
                    )
                    self._buf.close()

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.close()
                    return False

            return _WriteBuffer(buf, self._normalize_path(path), encoding, content_type, self)

        response = self._raw_request(
            'GET',
            'content',
            params={'path': self._normalize_path(path), 'encoding': 'raw'},
        )
        self._raise_for_status(response, 'content')
        body = BytesIO(response.content)
        if 'b' in mode:
            return body
        encoding = kwargs.get('encoding') or 'utf-8'
        return TextIOWrapper(body, encoding=encoding)

    def read_base64(self, path: str) -> bytes:
        if self._use_local_backend():
            with open(self._local_path(path), 'rb') as fh:
                return fh.read()
        data = self._request_json('content', path=self._normalize_path(path), encoding='base64')
        content = data.get('content') if isinstance(data, dict) else None
        if not isinstance(content, str):
            raise RuntimeError('remote-fs content base64 response missing content')
        return base64.b64decode(content)

    def materialize_dir(self, path: str, local_dir: str, **kwargs) -> Dict[str, Any]:
        normalized_root = self._normalize_path(path)
        if self._use_local_backend():
            source_dir = self._local_path(normalized_root)
            if not os.path.isdir(source_dir):
                raise NotADirectoryError(f'path is not a directory: {self._qualify_name(normalized_root)}')
            files: List[str] = []
            for root, _dirs, filenames in os.walk(source_dir):
                for filename in filenames:
                    rel = os.path.relpath(os.path.join(root, filename), source_dir).replace(os.sep, '/')
                    files.append(rel)
            return {
                'source_path': self._qualify_name(normalized_root),
                'local_dir': source_dir,
                'materialized': False,
                'file_count': len(files),
                'files': sorted(files),
            }
        files: List[str] = []

        def walk(current: str) -> None:
            for entry in self.ls(current, detail=True):
                entry_name = str((entry or {}).get('name') or '').strip()
                if not entry_name:
                    continue
                remote_name = self._normalize_path(entry_name)
                if str((entry or {}).get('type') or 'file') in ('directory', 'dir'):
                    walk(remote_name)
                    continue
                prefix = normalized_root.rstrip('/') + '/'
                rel_path = remote_name[len(prefix):] if remote_name.startswith(prefix) else remote_name.rsplit('/', 1)[-1]
                if not rel_path or rel_path.startswith('../') or '/..' in rel_path:
                    raise RuntimeError(f'remote-fs materialize got invalid relative path: {rel_path!r}')
                destination = os.path.join(local_dir, *rel_path.split('/'))
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with self.open(remote_name, 'rb') as src, open(destination, 'wb') as dst:
                    dst.write(src.read())
                files.append(rel_path)

        walk(normalized_root)
        return {
            'source_path': self._qualify_name(normalized_root),
            'local_dir': local_dir,
            'materialized': True,
            'file_count': len(files),
            'files': sorted(files),
        }
