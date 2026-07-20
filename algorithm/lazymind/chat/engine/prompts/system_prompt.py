from __future__ import annotations

from datetime import datetime, timezone as datetime_timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import guidance as _guidance
from .guidance import (
    ATTACHED_FILES_GUIDANCE,
    DEFAULT_SYSTEM_PROMPT,
    IMAGE_REFERENCE_MARKDOWN_GUIDANCE,
    KNOWLEDGE_EVIDENCE_CITATION_GUIDANCE,
    RESPONSE_LANGUAGE_GUIDANCE,
    SEARCH_GUIDANCE,
    TOOL_CALL_STATUS_GUIDANCE,
    WEB_SEARCH_GUIDANCE,
)
from .task_profile import TaskProfile

ANALYSIS_GUIDANCE = getattr(_guidance, 'ANALYSIS_GUIDANCE', '')
CLARIFICATION_GUIDANCE = getattr(_guidance, 'CLARIFICATION_GUIDANCE', '')
CLOUD_DOCUMENT_GUIDANCE = getattr(
    _guidance,
    'CLOUD_DOCUMENT_GUIDANCE',
    (
        'Use `FeishuWikiFS_search` and `FeishuWikiFS_find` for Feishu documents, and '
        '`NotionFS_search` and `NotionFS_find` for Notion pages, and `GoogleDriveFS_search` '
        'and `GoogleDriveFS_find` for Google Drive files. Feishu search searches all '
        'Wiki documents accessible to the authenticated account; do not ask the user to provide '
        'a space id, node id, document id, or local knowledge base identifier. These tools search '
        'the cloud document provider, not the local knowledge base.'
    ),
)
DECISION_PLANNING_GUIDANCE = getattr(_guidance, 'DECISION_PLANNING_GUIDANCE', '')
DELIVERABLE_GUIDANCE = getattr(_guidance, 'DELIVERABLE_GUIDANCE', {})
FRESH_RESEARCH_GUIDANCE = getattr(_guidance, 'FRESH_RESEARCH_GUIDANCE', '')
LEARNING_GUIDANCE = getattr(_guidance, 'LEARNING_GUIDANCE', '')
REQUEST_ANALYSIS_GUIDANCE = getattr(_guidance, 'REQUEST_ANALYSIS_GUIDANCE', '')
SKILL_RESTRAINT_GUIDANCE = getattr(_guidance, 'SKILL_RESTRAINT_GUIDANCE', '')
TRANSFORMATION_GUIDANCE = getattr(_guidance, 'TRANSFORMATION_GUIDANCE', '')

_KNOWLEDGE_EVIDENCE_GROUPS = {'kb', 'temp_kb'}
_DEFAULT_UI_LOCALE = 'zh-CN'
_CJK_PATTERN = re.compile(r'[\u3400-\u9fff]')
_LATIN_PATTERN = re.compile(r'[A-Za-z]')
_URL_PATTERN = re.compile(r'https?://\S+|www\.\S+', re.IGNORECASE)
_EXPLICIT_LANGUAGE_PATTERNS = (
    (
        'Chinese',
        re.compile(
            r'(?:请|始终|默认|务必|改为|切换到|使用|用|以).{0,16}'
            r'(?:中文|汉语|普通话|Chinese|Mandarin)|'
            r'(?:语言偏好|首选语言|默认语言).{0,8}(?:中文|汉语|普通话|Chinese|Mandarin)|'
            r'(?:preferred language|language preference).{0,8}(?:Chinese|Mandarin)|'
            r'(?:reply|answer|respond|write|speak|use|using|in).{0,16}'
            r'(?:in\s+)?(?:Chinese|Mandarin)|'
            r'(?:Chinese|Mandarin)\s+please',
            re.IGNORECASE,
        ),
    ),
    (
        'English',
        re.compile(
            r'(?:请|始终|默认|务必|改为|切换到|使用|用|以).{0,16}'
            r'(?:英文|英语|English)|'
            r'(?:语言偏好|首选语言|默认语言).{0,8}(?:英文|英语|English)|'
            r'(?:preferred language|language preference).{0,8}English|'
            r'(?:reply|answer|respond|write|speak|use|using|in).{0,16}'
            r'(?:in\s+)?English|'
            r'English\s+please',
            re.IGNORECASE,
        ),
    ),
)


def _get_ui_locale(environment_context: dict | None = None) -> str:
    if isinstance(environment_context, dict):
        locale = str(environment_context.get('locale') or '').strip()
        if locale:
            return locale
    return _DEFAULT_UI_LOCALE


def _explicit_language(text: object) -> str:
    value = str(text or '').strip()
    if not value:
        return ''
    for language, pattern in _EXPLICIT_LANGUAGE_PATTERNS:
        if pattern.search(value):
            return language
    return ''


def _dominant_language(text: object) -> str:
    value = _URL_PATTERN.sub(' ', str(text or '')[:2000])
    cjk_count = len(_CJK_PATTERN.findall(value))
    latin_count = len(_LATIN_PATTERN.findall(value))
    if cjk_count >= 2 and cjk_count * 2 >= latin_count:
        return 'Chinese'
    if latin_count >= 4 and latin_count > cjk_count * 2:
        return 'English'
    return ''


def _conversation_language(history: list[dict] | None = None) -> str:
    recent_user_messages = []
    for message in reversed(history or []):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str) and content.strip():
            recent_user_messages.append(content)
        if len(recent_user_messages) >= 3:
            break
    return _dominant_language('\n'.join(reversed(recent_user_messages)))


def _locale_language(locale: str) -> str:
    normalized = locale.strip().lower()
    if normalized.startswith('zh'):
        return 'Chinese'
    if normalized.startswith('en'):
        return 'English'
    return locale.strip() or _DEFAULT_UI_LOCALE


def _resolve_response_language(
    *,
    current_query: str | None = None,
    conversation_history: list[dict] | None = None,
    user_preference: str | None = None,
    environment_context: dict | None = None,
) -> tuple[str, str]:
    current_instruction = _explicit_language(current_query)
    if current_instruction:
        return current_instruction, 'explicit instruction in the current request'

    saved_preference = _explicit_language(user_preference)
    if saved_preference:
        return saved_preference, 'explicit saved user preference'

    request_language = _dominant_language(current_query)
    if request_language:
        return request_language, 'dominant language of the current request'

    history_language = _conversation_language(conversation_history)
    if history_language:
        return history_language, 'dominant language of recent user messages'

    locale = _get_ui_locale(environment_context)
    return _locale_language(locale), f'default UI locale {locale}'


def _build_response_language_prompt(
    environment_context: dict | None = None,
    *,
    current_query: str | None = None,
    conversation_history: list[dict] | None = None,
    user_preference: str | None = None,
) -> str:
    language, source = _resolve_response_language(
        current_query=current_query,
        conversation_history=conversation_history,
        user_preference=user_preference,
        environment_context=environment_context,
    )
    return (
        f'{RESPONSE_LANGUAGE_GUIDANCE}\n'
        f'Default UI locale for this request: {_get_ui_locale(environment_context)}.\n'
        f'Selected response language for this turn: {language} ({source}).\n'
        f'Use {language} for all user-visible natural-language text in this turn.'
    )


def _format_user_time(time_now: object, timezone: object) -> str:
    raw_time = str(time_now).strip()
    if not raw_time:
        return ''

    timezone_name = str(timezone).strip() if timezone is not None else ''
    try:
        normalized_time = raw_time[:-1] + '+00:00' if raw_time.endswith('Z') else raw_time
        parsed_time = datetime.fromisoformat(normalized_time)
        if parsed_time.tzinfo is None:
            parsed_time = parsed_time.replace(tzinfo=datetime_timezone.utc)
        if timezone_name:
            user_time = parsed_time.astimezone(ZoneInfo(timezone_name))
            return f'{user_time:%Y-%m-%d %H:%M:%S} ({timezone_name})'
        return parsed_time.isoformat()
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return raw_time


def _build_environment_context_prompt(environment_context: dict | None = None) -> str:
    time_now = None
    timezone = None
    if isinstance(environment_context, dict):
        time_info = environment_context.get('time') or {}
        if isinstance(time_info, dict):
            time_now = time_info.get('now')
            timezone = time_info.get('timezone')

    user_time = _format_user_time(time_now, timezone) if time_now else ''
    if not user_time:
        return ''

    return f'## Environment Context\nCurrent user time: {user_time}'


def _build_attached_files_prompt(files: list | None = None) -> str:
    clean = [str(path).strip() for path in (files or []) if str(path).strip()]
    if not clean:
        return ''
    lines = ['## Attached Files']
    lines.extend(f'- {path}' for path in clean)
    return '\n'.join(lines) + '\n\n' + ATTACHED_FILES_GUIDANCE


def _task_profile_system_parts(task_profile: TaskProfile | None) -> list[str]:
    if task_profile is None:
        return []
    outcomes = {task_profile.primary_outcome, *task_profile.secondary_outcomes}
    parts = []
    if 'learn' in outcomes:
        parts.append(LEARNING_GUIDANCE)
    if task_profile.research_required or task_profile.freshness == 'current':
        parts.append(FRESH_RESEARCH_GUIDANCE)
    if outcomes.intersection({'decide', 'plan'}):
        parts.append(DECISION_PLANNING_GUIDANCE)
    if 'analyze' in outcomes:
        parts.append(ANALYSIS_GUIDANCE)
    if 'transform' in outcomes:
        parts.append(TRANSFORMATION_GUIDANCE)
    deliverables = [task_profile.deliverable_kind, *task_profile.secondary_deliverables][:2]
    contracts = [DELIVERABLE_GUIDANCE[item] for item in deliverables if item in DELIVERABLE_GUIDANCE]
    if contracts and not (
        task_profile.complexity == 'simple' and task_profile.deliverable_kind == 'direct_answer'
    ):
        parts.append('# Deliverable contract\n' + '\n'.join(contracts))
    if task_profile.skill_mode != 'explicit':
        parts.append(SKILL_RESTRAINT_GUIDANCE)
    assessment = task_profile.request_assessment
    if assessment.status != 'ready' or task_profile.complexity == 'compound':
        parts.append(REQUEST_ANALYSIS_GUIDANCE)
    if assessment.interaction_need == 'blocking':
        parts.append(CLARIFICATION_GUIDANCE)
    return parts


def _task_profile_runtime_sections(task_profile: TaskProfile | None) -> list[tuple[str, str]]:
    if task_profile is None:
        return []
    sections = []
    excluded = task_profile.excluded_resources
    excluded_lines = [
        *(f'- Skill: {value}' for value in excluded.skill_names),
        *(f'- Knowledge base: {value}' for value in excluded.knowledge_base_ids),
        *(f'- Plugin: {value}' for value in excluded.plugin_refs),
    ]
    if excluded_lines:
        sections.append(('Resource Usage Policy', '\n'.join([
            'Do not use, invoke, cite, or rely on these resources in this turn:',
            *excluded_lines,
        ])))
    assessment = task_profile.request_assessment
    if assessment.status != 'ready':
        issue_lines = [
            f'- {issue.issue_type} ({issue.impact}): {issue.description} '
            f'[evidence: {issue.evidence}]'
            for issue in assessment.issues
        ]
        question_lines = [
            f'- {question.question}'
            + (f' Options: {", ".join(question.options)}.' if question.options else '')
            + (f' Recommended: {question.recommended}.' if question.recommended else '')
            for question in assessment.clarification_questions
        ]
        sections.append(('Request Assessment', '\n'.join([
            f'Status: {assessment.status}',
            f'Interaction need: {assessment.interaction_need}',
            *issue_lines,
            *question_lines,
        ])))
    return sections


def build_system_prompt(
    active_groups: set[str],
    *,
    environment_context: dict | None = None,
    use_memory: bool = True,
    user_preference: str | None = None,
    memory: str | None = None,
    files: list | None = None,
    current_query: str | None = None,
    conversation_history: list[dict] | None = None,
    task_profile: TaskProfile | None = None,
    dynamic_prompt_modules: bool = False,
) -> str:
    prompt_parts = [
        DEFAULT_SYSTEM_PROMPT,
        _build_response_language_prompt(
            environment_context,
            current_query=current_query,
            conversation_history=conversation_history,
            user_preference=user_preference,
        ),
    ]
    environment_prompt = _build_environment_context_prompt(environment_context)
    if environment_prompt:
        prompt_parts.append(environment_prompt)
    attached_files_prompt = _build_attached_files_prompt(files)
    if attached_files_prompt:
        prompt_parts.append(attached_files_prompt)
    if dynamic_prompt_modules:
        prompt_parts.extend(_task_profile_system_parts(task_profile))
        prompt_parts.extend(
            f'## {title} [AUTHORITATIVE]\n{content}'
            for title, content in _task_profile_runtime_sections(task_profile)
        )
    if use_memory:
        if isinstance(user_preference, str) and user_preference.strip():
            prompt_parts.append('## User Profile / Preferences\n' + user_preference.strip())
        if isinstance(memory, str) and memory.strip():
            prompt_parts.append(f'## Agent Working Memory\n{memory.strip()}')
    if active_groups:
        prompt_parts.append(TOOL_CALL_STATUS_GUIDANCE)
    if active_groups & _KNOWLEDGE_EVIDENCE_GROUPS:
        prompt_parts.extend([SEARCH_GUIDANCE, KNOWLEDGE_EVIDENCE_CITATION_GUIDANCE])
    if 'web_search' in active_groups:
        prompt_parts.append(WEB_SEARCH_GUIDANCE)
    if active_groups.intersection({'feishu', 'notion', 'google_drive'}):
        prompt_parts.append(CLOUD_DOCUMENT_GUIDANCE)
    if files or active_groups.intersection({'image_generator', 'image_editor'}):
        prompt_parts.append(IMAGE_REFERENCE_MARKDOWN_GUIDANCE)
    return '\n\n'.join(prompt_parts)


def add_standard_system_sections(builder, has_tools: bool, **kwargs):
    """Compatibility adapter for callers that still use the structured prompt builder."""
    task_profile = kwargs.get('task_profile')
    prompt = build_system_prompt(
        {'tools'} if has_tools else set(),
        environment_context=kwargs.get('environment_context'),
        use_memory=kwargs.get('use_memory', True),
        user_preference=kwargs.get('user_preference'),
        memory=kwargs.get('memory'),
        current_query=kwargs.get('current_query'),
        conversation_history=kwargs.get('conversation_history'),
        task_profile=task_profile,
        dynamic_prompt_modules=kwargs.get('dynamic_prompt_modules', False),
    )
    builder.system('standard_prompt', '', prompt, 'platform.guidance', priority=10)
    for index, (title, content) in enumerate(_task_profile_runtime_sections(task_profile)):
        builder.runtime(
            f'task_runtime_{index}', title, content, 'runtime.task',
            priority=4 + index, authoritative=True, content_kind='state',
        )
    return builder
