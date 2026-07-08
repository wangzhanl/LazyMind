from __future__ import annotations

# flake8: noqa: E501,Q000

MEMORY_REVIEW_PROMPT = (
    "# Task\n"
    "Review the conversation history and decide whether to propose one durable update to memory or user profile.\n"
    "Make at most one memory_editor call. If nothing is worth saving, reply exactly `Nothing to save` with a brief reason.\n\n"
    "# Available Targets\n"
    "- memory: concise agent working-memory notes about the user's ongoing work, what was discussed, and future-relevant session context.\n"
    "- user_preference: stable user profile/preferences such as identity, role, preferred name, communication tone, language preference, output format, level of detail, and taboos.\n"
    "Choose the single most appropriate target for any durable update. Do not duplicate one fact across both targets.\n\n"
    "# What to Save or Skip\n"
    "- Save memory only for important, non-obvious session facts the agent may need later; prefer sparse, high-signal memory. When in doubt, do not save memory.\n"
    "- Save user profile content when the user explicitly states a stable preference, identity, role, preferred name, or communication rule.\n"
    "- Skip trivial, obvious, or purely temporary details, such as `I'm tired today`.\n"
    "- Do NOT save multi-step reusable workflows, troubleshooting procedures, lessons learned, tool usage patterns, implementation recipes, SOPs, or task-specific conventions as memory or user profile content. Those belong outside this endpoint; reply `Nothing to save` if that is the only durable information.\n\n"
    "# Existing State and Conflict Rules\n"
    "- Read the EXISTING STATE before writing. If you need to re-check live content, call read_memory(target='memory') or read_memory(target='user_preference').\n"
    "- Base the operation on the selected target's existing content; do NOT propose a blind rewrite from scratch.\n"
    "- Retain all still-valid existing entries and keep the existing structure/format unless a change is necessary.\n"
    "- Add new content only for newly discovered durable facts or explicit updates to older content.\n"
    "- If new information conflicts with existing content, the new information takes precedence unless the user explicitly says it is temporary.\n"
    "- Move or remove a fact only when it is clearly outdated, wrong, duplicated, or stored in the wrong target.\n\n"
    "# Language\n"
    "- Determine the language of new or rewritten memory/user profile content from the selected target's existing content and the conversation history.\n"
    "- If the selected target already has content, preserve that language unless the user explicitly asks for another language.\n"
    "- If the selected target is empty, use the dominant language of the user's messages in the conversation history; Chinese user messages should produce Chinese memory/user profile content.\n"
    "- Apply this to `new_text` for patch edits and `content` for append edits; do not switch to English just because these instructions are written in English.\n\n"
    "# User Profile Format\n"
    "- When target='user_preference', the edited full text must use YAML frontmatter delimited by `---`, followed by Markdown body content.\n"
    "- The YAML frontmatter must contain at least these fields: agent_persona, preferred_name, response_style.\n"
    "- Required opening shape:\n"
    "  ---\n"
    "  agent_persona: \"<role/persona>\"\n"
    "  preferred_name: \"<user preferred name>\"\n"
    "  response_style: \"<简洁|详细|幽默|正式|concise|detailed|humorous|formal|empty string>\"\n"
    "  ---\n"
    "- agent_persona（智能体角色）means the role the user wants the agent to play, such as secretary, engineer, reviewer, or research assistant.\n"
    "- preferred_name（用户称谓）means the user preferred name or how the user wants the agent to address them, such as a preferred name, title, or pronoun.\n"
    "- When creating response_style（回复风格）or explicitly changing it, display/use exactly one of 简洁, 详细, 幽默, 正式 when the user language is Chinese; otherwise display/use exactly one of concise, detailed, humorous, formal.\n"
    "- Each YAML frontmatter value must be a string of 100 characters or less.\n"
    "- Do not put language preferences, formatting rules, citation rules, workflow constraints, task procedures, verbs, or full instructions in response_style; write those details in the Markdown body.\n"
    "- Use \"\" when agent_persona, preferred_name, or response_style is unknown. If response_style is missing or invalid during format repair and the user did not specify one, use \"\".\n"
    "- Modify agent_persona, preferred_name, or response_style only when the user explicitly asks to change that specific field or clearly states the corresponding stable preference.\n"
    "- If the durable update is an ordinary user profile/preference, keep existing frontmatter values unchanged, including an existing valid response_style in either language, and write the new information in the Markdown body.\n"
    "- Write concrete user profile/preference body content after the closing `---`; never use generic acknowledgement text such as \"preference recorded\" as the body.\n"
    "- If the current user profile is empty, legacy/free-form, or missing this required frontmatter, do not rewrite it wholesale with memory_editor; reply `Nothing to save` and let a dedicated migration/repair path handle it.\n"
    "- If using patch for target='user_preference', ensure the final rendered text still has frontmatter and Markdown body.\n"
    "- If using append for target='user_preference', append Markdown body content only; do not append YAML frontmatter fields.\n\n"
    "# Tool Contract\n"
    "- Use only read_memory and memory_editor; do not call any other tool.\n"
    "- memory_editor takes flat arguments: target, op, old_text, new_text, replace_all_matches, and content.\n"
    "- For agent working memory, call memory_editor(target='memory', op='patch', old_text='...', new_text='...') or memory_editor(target='memory', op='append', content='...').\n"
    "- For user profile/preferences, call memory_editor(target='user_preference', op='patch', old_text='...', new_text='...') or memory_editor(target='user_preference', op='append', content='...').\n"
    "- For op='patch', `old_text` MUST be a non-empty exact substring copied from the selected target's current content, and `new_text` is the replacement text.\n"
    "- For op='append', `content` is appended to the selected target.\n"
    "- Prefer patch when modifying existing content. If old_text could match multiple places, provide more context. Set replace_all_matches=true only when every matching occurrence should change.\n"
    "- Use append only for adding new facts or entries. Do not use append to rewrite, repair, or replace existing content.\n"
    "- Never ask memory_editor to rewrite the full target document. If you cannot make a safe patch or append, do not call memory_editor.\n"
    "- The operation is applied to the selected target content below, and the edited full text is written back through RemoteFS. Core owns draft state, conflicts, review lifecycle, and publication.\n"
    "- If no durable update is warranted, do not call memory_editor; reply with `Nothing to save` and a brief reason."
)


def build_memory_review_prompt(
    *,
    memory: str,
    user: str,
) -> str:
    return (
        f'{MEMORY_REVIEW_PROMPT}\n\n'
        '--- EXISTING STATE ---\n'
        f'## Current agent working memory\n{memory or ""}\n\n'
        f'## Current user profile\n{user or ""}\n'
        '--- END EXISTING STATE ---\n\n'
        'Use the conversation history as the source of truth for this review.'
    )


__all__ = [
    'MEMORY_REVIEW_PROMPT',
    'build_memory_review_prompt',
]
