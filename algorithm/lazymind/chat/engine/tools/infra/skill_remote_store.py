from __future__ import annotations

import os
from typing import Any, Dict, Mapping

from lazymind.chat.integrations.remote_fs import RemoteFS
from lazymind.chat.engine.tools.infra.skill_operations import normalize_skill_package_path


def _remote_fs() -> RemoteFS:
    return RemoteFS()


def skill_package_dir(category: str, name: str) -> str:
    return f'remote://skills/{category}/{name}'


def skill_file_path(category: str, name: str, rel_path: str = 'SKILL.md') -> str:
    return f'{skill_package_dir(category, name)}/{normalize_skill_package_path(rel_path)}'


def _entry_name(entry: Dict[str, Any]) -> str:
    return str((entry or {}).get('name') or '').strip()


def _entry_type(entry: Dict[str, Any]) -> str:
    return str((entry or {}).get('type') or 'file').strip()


def _relative_to_package(package_dir: str, path: str) -> str:
    prefix = package_dir.rstrip('/') + '/'
    if path.startswith(prefix):
        return normalize_skill_package_path(path[len(prefix):])
    return normalize_skill_package_path(path.rsplit('/', 1)[-1])


def read_skill_file(category: str, name: str, rel_path: str = 'SKILL.md', *, fs=None) -> str:
    store = fs or _remote_fs()
    with store.open(skill_file_path(category, name, rel_path), 'r', encoding='utf-8', errors='replace') as fh:
        return fh.read()


def write_skill_file(category: str, name: str, rel_path: str, content: str, *, fs=None) -> None:
    store = fs or _remote_fs()
    store.write(skill_file_path(category, name, rel_path), content)


def remove_skill_file(category: str, name: str, rel_path: str, *, fs=None) -> None:
    store = fs or _remote_fs()
    store.rm(skill_file_path(category, name, rel_path), recursive=False)


def write_skill_files(category: str, name: str, files: Mapping[str, str], *, fs=None) -> None:
    store = fs or _remote_fs()
    for rel_path, content in files.items():
        write_skill_file(category, name, rel_path, content, fs=store)


def replace_skill_package_files(
    category: str,
    name: str,
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    fs=None,
) -> Dict[str, list[str]]:
    store = fs or _remote_fs()
    before_paths = set(before)
    after_paths = set(after)
    deleted = sorted(before_paths - after_paths)
    written = sorted(path for path in after_paths if before.get(path) != after.get(path))
    for rel_path in deleted:
        remove_skill_file(category, name, rel_path, fs=store)
    for rel_path in written:
        write_skill_file(category, name, rel_path, after[rel_path], fs=store)
    return {'written': written, 'deleted': deleted}


def list_skill_files(category: str, name: str, *, fs=None) -> Dict[str, str]:
    store = fs or _remote_fs()
    package_dir = skill_package_dir(category, name)
    files: Dict[str, str] = {}

    def walk(path: str) -> None:
        for entry in store.ls(path, detail=True):
            entry_name = _entry_name(entry)
            if not entry_name:
                continue
            if _entry_type(entry) in ('directory', 'dir'):
                walk(entry_name)
                continue
            rel_path = _relative_to_package(package_dir, entry_name)
            files[rel_path] = read_skill_file(category, name, rel_path, fs=store)

    walk(package_dir)
    return files


def materialize_skill_package(category: str, name: str, local_dir: str, *, fs=None) -> Dict[str, Any]:
    files = list_skill_files(category, name, fs=fs)
    for rel_path, content in files.items():
        destination = os.path.join(local_dir, *rel_path.split('/'))
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, 'w', encoding='utf-8') as fh:
            fh.write(content)
    return {
        'path': skill_package_dir(category, name),
        'local_dir': local_dir,
        'file_count': len(files),
        'files': sorted(files),
    }


def rename_skill_package(
    old_category: str,
    old_name: str,
    new_category: str,
    new_name: str,
    *,
    skill_content: str,
    fs=None,
) -> dict:
    store = fs or _remote_fs()
    old_path = skill_package_dir(old_category, old_name)
    new_path = skill_package_dir(new_category, new_name)
    store.move(old_path, new_path, recursive=True)
    write_skill_file(new_category, new_name, 'SKILL.md', skill_content, fs=store)
    return {
        'persisted': 'remote_fs',
        'path': new_path,
        'old_name': old_name,
        'old_category': old_category,
        'name': new_name,
        'category': new_category,
        'action': 'rename',
    }


def create_remote_skill(category: str, name: str, content: str) -> dict:
    path = skill_file_path(category, name, 'SKILL.md')
    _remote_fs().write(path, content)
    return {
        'persisted': 'remote_fs',
        'path': path,
        'name': name,
        'category': category,
        'action': 'create',
    }


def remove_remote_skill(category: str, name: str) -> dict:
    path = skill_package_dir(category, name)
    _remote_fs().rm(path, recursive=True)
    return {
        'persisted': 'remote_fs',
        'deleted': True,
        'path': path,
        'name': name,
        'category': category,
        'action': 'remove',
    }
