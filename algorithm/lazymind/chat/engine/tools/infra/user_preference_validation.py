from __future__ import annotations

import re
from typing import Any, Optional

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*(\n(.*))?$', re.DOTALL)
_FRONTMATTER_FIELDS = {'agent_persona', 'preferred_name', 'response_style'}
_MAX_FRONTMATTER_VALUE_LENGTH = 100


def parse_user_preference_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(content or '')
    if not match:
        return {}, content or ''

    yaml_text, body = match.group(1), match.group(3) or ''
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(yaml_text)
        if isinstance(parsed, dict):
            return parsed, body
    except Exception:
        pass

    return {}, body


def validate_user_preference_content(content: str) -> Optional[str]:
    if not content or not content.strip():
        return "user_preference requires a non-empty 'content'."

    frontmatter, body = parse_user_preference_frontmatter(content)
    if not frontmatter:
        return 'user_preference must contain YAML frontmatter.'
    if 'agent_persona' not in frontmatter:
        return "Frontmatter must include 'agent_persona'."
    if 'preferred_name' not in frontmatter:
        return "Frontmatter must include 'preferred_name'."
    if 'response_style' not in frontmatter:
        return "Frontmatter must include 'response_style'."
    extra_fields = set(frontmatter) - _FRONTMATTER_FIELDS
    if extra_fields:
        fields = ', '.join(sorted(extra_fields))
        return f'Frontmatter contains unsupported fields: {fields}.'
    for field in sorted(_FRONTMATTER_FIELDS):
        value = frontmatter.get(field)
        if not isinstance(value, str):
            return f"Frontmatter '{field}' must be a string."
        if len(value) > _MAX_FRONTMATTER_VALUE_LENGTH:
            return (
                f"Frontmatter '{field}' must be "
                f'{_MAX_FRONTMATTER_VALUE_LENGTH} characters or less.'
            )
    if not body.strip():
        return 'user_preference must have Markdown body content after frontmatter.'
    return None
