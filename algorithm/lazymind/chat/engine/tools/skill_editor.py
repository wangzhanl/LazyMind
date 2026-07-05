from typing import Any, Dict, List, Literal, Optional

import lazyllm

from lazymind.chat.engine.tools.infra import (
    build_skill_identity,
    create_remote_skill,
    is_writable_skill_source,
    list_all_skill_entries,
    list_skill_files,
    normalize_skill_category,
    parse_skill_frontmatter,
    remove_remote_skill,
    rename_skill_package,
    replace_skill_package_files,
    tool_error,
    tool_success,
    validate_skill_content,
    validate_skill_name,
)
from lazymind.chat.engine.tools.infra.skill_operations import (
    SkillEditOperation,
    apply_skill_package_operations,
)
from lazymind.config import config as _cfg


_CREATE_SUCCESS_RESULT = {
    'status': 'created',
    'message': 'Skill was created and is now active.',
}
_MODIFY_SUCCESS_RESULT = {
    'status': 'modified',
    'message': 'Skill package was modified and is now active.',
}
_RENAME_SUCCESS_RESULT = {
    'status': 'renamed',
    'message': 'Skill package was renamed and is now active.',
}
_REMOVE_SUCCESS_RESULT = {
    'status': 'removed',
    'message': 'Skill was removed and is no longer active.',
}


def skill_editor(
    name: str,
    action: Literal['create', 'modify', 'rename', 'remove'],
    category: Optional[str],
    content: Optional[str] = None,
    operations: Optional[List[SkillEditOperation]] = None,
    reason: Optional[str] = None,
    new_name: Optional[str] = None,
    new_category: Optional[str] = None,
) -> Dict[str, Any]:
    """Manage skills by creating, modifying, renaming, or removing a skill package.

    Use this tool to curate reusable skills. It has four actions:

    - action='create': after completing a complex task (5+ tool calls),
      fixing a tricky error, or discovering a non-trivial workflow, save the
      approach as a new skill by passing the full SKILL.md body in
      content. The SKILL.md YAML frontmatter must include name, category, and
      description. A successful create takes effect immediately.
    - action='modify': when finding a skill package outdated, incomplete, or
      wrong, apply file operations to the remote skill package.
    - action='rename': when a skill identity must change, move the whole skill
      package and update SKILL.md frontmatter name/category. Do not change
      identity through action='modify'.
    - action='remove': when a skill is superseded or no longer correct,
      request its deletion. A successful remove takes effect immediately.

    Only skills with source=remote are writable. Skills with
    source=file or any other source are read-only; do not use this tool
    to modify or remove them.

    Both name and category are used as on-disk directory names, so they must
    not contain whitespace or slashes. The category argument must be a single
    path segment such as "engineering" or "coding"; do not nest categories like
    "engineering/railway". The layout is always category/name/SKILL.md.

    For modify and remove, derive category from the directory immediately above
    the skill_name directory in the skill path. For example, in
    ".../skills/testing/test-full-flow", name is "test-full-flow" and category
    is "testing". Preserve category/name identity during modify; use rename
    when category or name must change.

    Args:
        name: Skill name.
        action: Skill workflow to run. Use 'create' to create a new skill
            that takes effect immediately, 'modify' to edit an existing remote
            skill using the 'operations' argument, 'rename' to move an existing
            skill package to new_name/new_category, or 'remove' to delete it
            immediately.
        category: Skill category directory used to locate category/name/SKILL.md.
        content: Full SKILL.md content, including YAML frontmatter with
            name/category/description. ONLY for action='create'. Do NOT pass
            for action='modify' or 'remove'.
        operations: Ordered JSON file edit operations. ONLY for action='modify'.
            Do NOT pass for action='create' or 'remove'. Supported operations:

            - ``{"op": "patch_file", "path": "SKILL.md", "old_text": "...", "new_text": "..."}``:
              replace text in one package file. ``path`` defaults to SKILL.md.
            - ``{"op": "write_file", "path": "references/api.md", "content": "..."}``:
              create or overwrite one package file.
            - ``{"op": "delete_file", "path": "examples/old.md"}``:
              delete one supporting file. SKILL.md cannot be deleted.
        reason: Why the skill should be removed. ONLY for action='remove'.
        new_name: New skill name. ONLY for action='rename'.
        new_category: New skill category. ONLY for action='rename'. If omitted,
            the current category is kept.
    """
    lazyllm.LOG.info(
        '[skill_editor] called '
        f'name={name!r} action={action!r} '
        f'category={category!r} content_len={len(content) if content else 0} '
        f'operations_count={len(operations) if operations else 0}'
    )

    existing_skills = list_all_skill_entries(_cfg['skill_fs_url'])
    resolved = _resolve_skill_editor_identity(name, category, existing_skills, action)
    if resolved.get('error'):
        return tool_error('skill_editor', resolved['error'])
    normalized_category = resolved['category']
    name = resolved['name']
    skill_id = build_skill_identity(normalized_category or '', name)
    existing_skill = existing_skills.get(skill_id)
    lazyllm.LOG.info(
        '[skill_editor] lookup '
        f'skill_id={skill_id!r} '
        f'found={existing_skill is not None} '
        f'existing_keys={list(existing_skills.keys())!r}'
    )

    if action == 'create':
        content_error = validate_skill_content(content or '')
        if content_error:
            return tool_error(
                'skill_editor',
                content_error,
                log_message=f'[skill_editor] fail reason={content_error!r}',
            )
        if operations:
            return tool_error('skill_editor', "action='create' must not include 'operations'.")
        content_category, content_name = _skill_identity_from_content(content or '')
        if content_category != normalized_category or content_name != name:
            return tool_error(
                'skill_editor',
                'SKILL.md frontmatter name/category must match the tool name/category for create.'
            )
        if existing_skill:
            return tool_error('skill_editor', f"Skill {content_name!r} already exists in category {content_category!r}.")

        create_remote_skill(content_category, content_name, content or '')
        _record_runtime_delta('created', {'category': content_category, 'name': content_name})
        return tool_success('skill_editor', _CREATE_SUCCESS_RESULT)

    if action == 'modify':
        if content is not None:
            return tool_error('skill_editor', "action='modify' must not include 'content'; use 'operations'.")
        if not operations:
            return tool_error('skill_editor', "action='modify' requires a non-empty 'operations' list.")
        if not existing_skill:
            return tool_error(
                'skill_editor',
                f'Skill {name!r} does not exist in category {normalized_category!r}; '
                "use action='create' to add a new skill."
            )
        source = existing_skill.get('source', 'file')
        lazyllm.LOG.info(
            '[skill_editor] modify_check '
            f'source={source!r} '
            f'writable={is_writable_skill_source(source)}'
        )
        if not is_writable_skill_source(source):
            return tool_error(
                'skill_editor',
                f'Skill {name!r} in category {normalized_category!r} has read-only source '
                f'{source!r}; skill_editor can only modify remote skills.'
            )

        try:
            from lazymind.rewrite.base import UnprocessableContentError

            current_files = list_skill_files(normalized_category, name)
            if 'SKILL.md' not in current_files:
                current_files['SKILL.md'] = existing_skill.get('content') or ''
            edited_files, operation_payload = apply_skill_package_operations(
                current_files,
                operations,
            )
        except UnprocessableContentError as exc:
            return tool_error('skill_editor', str(exc))
        except Exception as exc:
            return tool_error('skill_editor', f'Failed to load or edit skill package: {exc}')

        edited_content = edited_files['SKILL.md']
        content_error = validate_skill_content(edited_content)
        if content_error:
            return tool_error('skill_editor', content_error)
        edited_category, edited_name = _skill_identity_from_content(edited_content)
        if edited_category != normalized_category or edited_name != name:
            return tool_error(
                'skill_editor',
                'action=\'modify\' cannot change SKILL.md frontmatter name/category; use action=\'rename\'.'
            )
        try:
            change_set = replace_skill_package_files(
                normalized_category,
                name,
                current_files,
                edited_files,
            )
        except Exception as exc:
            return tool_error('skill_editor', f'Failed to persist skill package changes: {exc}')

        _record_runtime_delta('touched', {'category': normalized_category, 'name': name})
        result = dict(_MODIFY_SUCCESS_RESULT)
        touched_files = sorted({op.get('path', 'SKILL.md') for op in operation_payload})
        result['touched_files'] = touched_files
        result['written_files'] = change_set['written']
        result['deleted_files'] = change_set['deleted']
        result['summary'] = reason or _operation_summary(operation_payload)
        return tool_success('skill_editor', result)

    if action == 'rename':
        if content is not None or operations:
            return tool_error('skill_editor', "action='rename' must not include 'content' or 'operations'.")
        if not existing_skill:
            return tool_error(
                'skill_editor',
                f'Skill {name!r} does not exist in category {normalized_category!r}; nothing to rename.'
            )
        source = existing_skill.get('source', 'file')
        if not is_writable_skill_source(source):
            return tool_error(
                'skill_editor',
                f'Skill {name!r} in category {normalized_category!r} has read-only source '
                f'{source!r}; skill_editor can only rename remote skills.'
            )
        target_name = str(new_name or '').strip()
        name_error = validate_skill_name(target_name)
        if name_error:
            return tool_error('skill_editor', f'new_name is invalid: {name_error}')
        target_category = normalize_skill_category(new_category if new_category is not None else normalized_category)
        if not target_category:
            return tool_error(
                'skill_editor',
                f'new_category {new_category!r} is invalid; it must be a single ASCII-safe path segment.'
            )
        if target_category == normalized_category and target_name == name:
            return tool_error('skill_editor', 'action=\'rename\' requires a different new_name or new_category.')
        target_id = build_skill_identity(target_category, target_name)
        if existing_skills.get(target_id):
            return tool_error('skill_editor', f'Skill {target_name!r} already exists in category {target_category!r}.')

        try:
            current_files = list_skill_files(normalized_category, name)
            skill_content = current_files.get('SKILL.md') or existing_skill.get('content') or ''
            renamed_content = _rewrite_skill_identity(skill_content, target_category, target_name)
        except Exception as exc:
            return tool_error('skill_editor', f'Failed to prepare skill rename: {exc}')
        content_error = validate_skill_content(renamed_content)
        if content_error:
            return tool_error('skill_editor', content_error)

        try:
            rename_skill_package(
                normalized_category,
                name,
                target_category,
                target_name,
                skill_content=renamed_content,
            )
        except Exception as exc:
            return tool_error('skill_editor', f'Failed to rename skill package: {exc}')

        payload = {
            'old': {'category': normalized_category, 'name': name},
            'new': {'category': target_category, 'name': target_name},
        }
        _record_runtime_delta('renamed', payload)
        result = dict(_RENAME_SUCCESS_RESULT)
        result.update(payload)
        return tool_success('skill_editor', result)

    if action == 'remove':
        if content is not None or operations:
            return tool_error('skill_editor', "action='remove' must not include 'content' or 'operations'.")
        if not existing_skill:
            return tool_error(
                'skill_editor',
                f'Skill {name!r} does not exist in category {normalized_category!r}; '
                'nothing to remove.'
            )
        source = existing_skill.get('source', 'file')
        if not is_writable_skill_source(source):
            return tool_error(
                'skill_editor',
                f'Skill {name!r} in category {normalized_category!r} has read-only source '
                f'{source!r}; skill_editor can only remove remote skills.'
            )

        remove_remote_skill(normalized_category, name)
        _record_runtime_delta('removed', {'category': normalized_category, 'name': name})
        return tool_success('skill_editor', _REMOVE_SUCCESS_RESULT)

    return tool_error(
        'skill_editor',
        f"Unknown action {action!r}; expected one of 'create', 'modify', 'rename', 'remove'."
    )


def _skill_identity_from_content(content: str) -> tuple[str, str]:
    frontmatter, _ = parse_skill_frontmatter(content)
    category = str(frontmatter.get('category') or '').strip()
    name = str(frontmatter.get('name') or '').strip()
    return category, name


def _operation_summary(operations: list[dict[str, Any]]) -> str:
    paths = sorted({str(op.get('path') or 'SKILL.md') for op in operations})
    return f'skill_editor package operations: {len(operations)} op(s), files={", ".join(paths)}'


def _resolve_skill_editor_identity(
    name: str,
    category: Optional[str],
    existing_skills: Dict[str, Dict[str, Any]],
    action: str,
) -> Dict[str, Any]:
    raw_name = str(name or '').strip()
    raw_category = str(category or '').strip()
    if raw_category and '/' in raw_name:
        return {'error': 'Pass either category plus name, or name as category/name; do not use both.'}

    if not raw_category and '/' in raw_name:
        parts = [part for part in raw_name.split('/') if part]
        if len(parts) != 2 or raw_name.startswith('/') or raw_name.endswith('/') or '//' in raw_name:
            return {'error': f"Skill key {raw_name!r} is invalid; expected category/name."}
        raw_category, raw_name = parts

    if raw_category:
        name_error = validate_skill_name(raw_name)
        if name_error:
            return {'error': name_error}
        normalized_category = normalize_skill_category(raw_category)
        if not normalized_category:
            return {
                'error': (
                    f'Category {raw_category!r} is invalid; it must be a single '
                    "ASCII-safe path segment (only letters, digits, '-', '_' "
                    "and '.'; no spaces, no Chinese, no '/')."
                )
            }
        return {'category': normalized_category, 'name': raw_name}

    if action == 'create':
        return {'error': "action='create' requires category, or name formatted as category/name."}

    name_error = validate_skill_name(raw_name)
    if name_error:
        return {'error': name_error}

    matches = [
        info for skill_id, info in existing_skills.items()
        if info.get('name') == raw_name or skill_id.rsplit('/', 1)[-1] == raw_name
    ]
    if not matches:
        return {'category': '', 'name': raw_name}
    if len(matches) > 1:
        match_keys = sorted(build_skill_identity(str(info.get('category') or ''), str(info.get('name') or ''))
                            for info in matches)
        return {
            'error': (
                f"Ambiguous skill name {raw_name!r}; use the full skill key "
                f"such as {match_keys[0]!r}. Matches: {', '.join(match_keys)}."
            )
        }
    match = matches[0]
    return {
        'category': str(match.get('category') or ''),
        'name': str(match.get('name') or raw_name),
    }


def _rewrite_skill_identity(content: str, category: str, name: str) -> str:
    frontmatter, body = parse_skill_frontmatter(content)
    if not frontmatter:
        raise ValueError('SKILL.md must contain YAML frontmatter.')
    frontmatter = dict(frontmatter)
    frontmatter['category'] = category
    frontmatter['name'] = name
    import yaml  # type: ignore

    yaml_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    return f'---\n{yaml_text}\n---\n{body}'


def _record_runtime_delta(kind: str, payload: dict[str, Any]) -> None:
    agentic_config = lazyllm.globals['agentic_config']
    delta = agentic_config.get('skill_runtime_delta')
    if not isinstance(delta, dict):
        delta = {'version': 0, 'created': [], 'renamed': [], 'removed': [], 'touched': []}
        agentic_config['skill_runtime_delta'] = delta
    for key in ('created', 'renamed', 'removed', 'touched'):
        if not isinstance(delta.get(key), list):
            delta[key] = []
    delta['version'] = int(delta.get('version') or 0) + 1
    delta[kind].append(payload)
