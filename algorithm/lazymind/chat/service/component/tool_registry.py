from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any, Callable

import docstring_parser
import lazyllm
from lazyllm.tools.fs.supplier.feishu import FeishuWikiFS
from lazyllm.tools.fs.supplier.googledrive import GoogleDriveFS
from lazyllm.tools.fs.supplier.notion import NotionFS
from lazyllm.tools.tools.search import (
    ArxivSearch,
    BingSearch,
    BochaSearch,
    GoogleSearch,
    SciverseSearch,
    TavilySearch,
    WikipediaSearch,
)

from lazymind.chat.engine.tools import (
    KBToolGroup,
    ExternalDBToolGroup,
    LocalFSToolGroup,
    SystemQueryToolGroup,
    WriterToolGroup,
    calculator,
    image_editor,
    image_generator,
    kb_tmp_search,
    memory_editor,
    read_memory,
    SkillEditorToolGroup,
    url_fetch,
    video_generator,
    video_to_gif,
    vision_extractor,
    vocab_learn,
)
from lazymind.model_config import is_model_role_available

SystemPromptAppendix = dict[str, str | tuple[str, ...]]
SystemPromptAppendixProvider = Callable[[], SystemPromptAppendix | None]
SYSTEM_PROMPT_APPENDIX_SECTIONS = ('tool_policy', 'safety', 'output_contract', 'response_policy')

IMAGE_MARKDOWN_OUTPUT_APPENDIX: SystemPromptAppendix = {
    'output_contract': (
        '# Image path formatting (mandatory)\n'
        'When showing images in your answer, you MUST copy the `image_markdown` field from '
        'tool results verbatim when it is available. \n'
        'If `image_markdown` is absent, copy the `image_url` or signed `text` field that '
        'starts with `/static-files/` exactly.\n'
        'Rules:\n'
        '- Use Markdown image syntax only: `![alt](/static-files/...?expires=...&sig=...)`.\n'
        '- NEVER invent hosts or prefixes (`https://ext.lazymind.ai`, `agent-cdn.minimax.io`, '
        'OCR ports, CDN tool_output URLs, etc.).\n'
        '- NEVER rewrite `/static-files/` paths into `http://` or `https://` URLs.\n'
        '- Do not use MiniMax/agent CDN links for local images; they are invalid for this UI.\n'
        '- Do not paste bare filesystem paths (`/var/lib/lazymind/uploads/...`) in answers.',
    ),
}
VIDEO_MARKDOWN_OUTPUT_APPENDIX: SystemPromptAppendix = {
    'output_contract': (
        'When a tool result contains `video_markdown`, copy it verbatim into the final answer '
        '(or use `video_url` when markdown is absent). Do not invent or rewrite signed URLs.',
    ),
}
KNOWLEDGE_CITATION_OUTPUT_APPENDIX: SystemPromptAppendix = {
    'output_contract': (
        '# Knowledge evidence citation rules\n'
        'When answering with evidence retrieved from a knowledge base or uploaded '
        'document index, cite using the original `[[document.chunk]]` markers present '
        'in the retrieved evidence. Do not invent, rewrite, or fabricate citation markers.',
    ),
}
ATTACHED_FILES_TOOL_POLICY_APPENDIX: SystemPromptAppendix = {
    'tool_policy': (
        '# Attached file rules\n'
        'Attachments are listed for reference only — do NOT parse or read them automatically.\n'
        '- `find_user_attachment(filename, turn=N)`: get path/url to pass to image tools, plugins, '
        '`vision_extractor`, or `save_plugin_artifact`. Prefer this for images when the task is '
        'visual (edit, generate, plugin) or you only need the file location.\n'
        '- `read_user_attachment(filename, turn=N)`: extract TEXT — OCR for pdf/doc/docx/pptx, or a '
        'text description via vision for images. Use only when you need document text or a textual '
        'answer about image content (e.g. "what does this document say", "describe this diagram").\n'
        'Supported uploads: png, jpg, jpeg, pdf, doc, docx, pptx.\n'
        '- Default to the current turn (marked 当前轮次) when the user says '
        '"this image / 这张图 / 这个文件" without naming a turn.\n'
        '- For knowledge-base questions about indexed documents, you may also use '
        '`kb_tmp_search` or other `kb_*` tools when appropriate.',
    ),
}
ASK_USER_TOOL_POLICY_APPENDIX: SystemPromptAppendix = {
    'tool_policy': (
        '# User clarification and confirmation rules (mandatory)\n'
        'Whenever you need the user to answer a question before you can continue—including '
        'clarification, confirmation, approval, choosing among options, or supplying missing '
        'information—you MUST call `ask_user`. Never ask a question that requires a user response '
        'as plain assistant text, in a status update, or in the final answer. If you can proceed '
        'safely with a reasonable assumption and do not actually need a response, do not ask. '
        'Treat all of these as requiring `ask_user`: asking the user to choose A or B; asking what '
        'they want to do next; collecting goals, preferences, constraints, or missing details; '
        'requesting confirmation, approval, or permission; giving a quiz, exercise, interview, or '
        'knowledge check; and ending with an invitation that expects a reply. Examples include '
        '"Do you want the answer now or time to think?", "Are you asking for A or B?", '
        '"Which option should we use?", "Would you like me to continue?", and "Please tell me your '
        'specific intent." A question mark is not required: imperatives such as "Choose one", '
        '"Tell me your preference", and "Confirm before I continue" also require `ask_user`. '
        'Rhetorical questions that expect no answer do not require it. '
        'Calling `ask_user` ends the current turn; continue only after the user answers.',
    ),
}
KNOWLEDGE_SEARCH_TOOL_POLICY_APPENDIX: SystemPromptAppendix = {
    'tool_policy': (
        "# Selected Knowledge Base Rules (CRITICAL — follow strictly)\n"
        "The user selected or @mentioned one or more knowledge bases in this request. "
        "This is an explicit instruction to search them, not merely permission to do so. "
        "Concrete methods such as `KBToolkit_kb_search` and `KBToolkit_kb_keyword_search` "
        "are available, so call the appropriate search method directly. "
        "Your first substantive action for the turn MUST be one of those searches. Do not answer "
        "from memory, announce that you could search later, ask whether you should search, or start "
        "a plugin before searching. Use the knowledge-base search method FIRST for every retrieval "
        "need — no exceptions. Do not skip it because you think the web might have "
        "better information, or because the topic seems general, popular, or common "
        "knowledge. The knowledge base is your primary evidence source.\n\n"
        "Only after the knowledge-base search returns zero results or explicitly irrelevant results "
        "may you fall back to provider-specific search tools. "
        "You MUST NOT use any non-knowledge-base retrieval tool before trying knowledge-base tools.\n\n"
        "**Keyword search vs semantic search — which one to use:**\n"
        "When the user mentions a specific document name (e.g., 'xxx.pdf', 'report.docx', "
        "'slides.pptx') and asks about particular terms, phrases, or content within that "
        "document, prefer `kb_keyword_search` with `target=<document name>`, "
        "`target_type='file_name'`, and `keyword=<specific terms>`. This is faster and more precise "
        "for document-scoped exact matching.\n"
        "For `keyword`, extract the core term(s) the user is asking about (e.g., a single "
        "word or short phrase like 'file1' or 'Redis timeout'), not the entire query "
        "sentence. If the first attempt returns zero results, try a shorter or alternative "
        "keyword before considering fallback.\n"
        "When the keyword search returns results, answer directly from them — do not "
        "follow up with semantic search unless the returned content is clearly irrelevant "
        "or empty.\n"
        "Use semantic search only for open-ended queries where no specific document "
        "is named. If keyword search returns zero results after trying alternative "
        "keywords, fall back to semantic search.\n\n"
        "When the user gives a concrete URL or asks you to inspect a specific page, "
        "still try the knowledge-base search first; use `url_fetch` only when the knowledge base has "
        "no relevant result.\n\n"
        "For papers, research topics, arXiv ids, abstracts, or author-related questions, "
        "still try the knowledge-base search first; after knowledge-base evidence is unavailable or "
        "insufficient, prefer `AcademicSearchToolkit` over general web search tools. "
        "When answering with knowledge-base evidence, cite with the original `[[document.chunk]]` "
        "markers. When answering with web search tools, `url_fetch`, "
        "or `AcademicSearchToolkit`, do not "
        "fabricate `[[document.chunk]]`; instead, mention the source title or URL plainly.\n"
    ),
}
WEB_SEARCH_TOOL_POLICY_APPENDIX: SystemPromptAppendix = {
    'tool_policy': (
        '# Web Search Tool Rules\n'
        'When using `web_search`, the `query` must represent one search intent. '
        'If the user asks to search multiple unrelated keywords or topics, call '
        '`web_search` separately for each keyword/topic. Do not combine unrelated '
        'terms into one `query` with spaces, commas, punctuation, or list-like text.',
    ),
}
MEMORY_READER_TOOL_POLICY_APPENDIX: SystemPromptAppendix = {
    'tool_policy': (
        '# Conversation history versus persistent memory\n'
        'Conversation history is already included in the model messages and is the authoritative '
        'source for earlier turns in the current chat. Resolve short follow-ups and omitted subjects '
        'from that history. Do not call `read_memory` to inspect, summarize, or recover the current '
        'conversation. `read_memory` only reads optional cross-conversation notes or user-profile '
        'content; an empty result never implies that chat history is missing.'
    ),
}
CLOUD_DOCUMENT_TOOL_POLICY_APPENDIX: SystemPromptAppendix = {
    'tool_policy': (
        '# Cloud document link rules\n'
        'When the user provides a Feishu/Lark document URL, use the Feishu file-system tools '
        'to resolve the link and read the document before summarizing or analyzing it.\n'
        'When the user provides a Notion URL (`notion.so`, `notion.site`, `notion.com`, or '
        '`app.notion.com`), use the Notion file-system tools first. Prefer resolving the '
        'link, then reading with references when the task asks for analysis, summary, or '
        'linked-page context. Do not fall back to generic URL fetching for private Notion '
        'pages unless Notion tools are unavailable or unauthorized.\n'
        'When the user provides a Google Drive or Google Workspace document URL '
        '(`drive.google.com` or `docs.google.com`), use the Google Drive file-system tools '
        'instead of generic URL fetching.',
    ),
}


@dataclass
class ToolGroupConfig:
    name: str
    label: str
    description: str
    instance: Any
    label_en: str = ''
    description_en: str = ''
    model_role: str | None = None
    key_source: Callable[[], Any] | None = None
    pick_first_valid: bool = False
    capability_id: str = ''
    equivalence_scope: str = 'infrastructure'
    provider_id: str = ''
    product_id: str = ''
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    required_config: list[str] | None = None
    appendix_system_prompt: SystemPromptAppendix | SystemPromptAppendixProvider | None = None

    def __post_init__(self) -> None:
        if self.pick_first_valid and not isinstance(self.instance, (list, tuple)):
            raise TypeError(
                'instance must be a list or tuple when pick_first_valid is True, '
                f'got {type(self.instance).__name__}'
            )
        if callable(self.appendix_system_prompt):
            return
        self._validate_appendix(self.appendix_system_prompt)

    @staticmethod
    def _validate_appendix(appendix: SystemPromptAppendix | None) -> None:
        for section, values in (appendix or {}).items():
            if section not in SYSTEM_PROMPT_APPENDIX_SECTIONS:
                raise ValueError(
                    f'unsupported appendix_system_prompt section {section!r}; '
                    f'expected one of {SYSTEM_PROMPT_APPENDIX_SECTIONS}'
                )
            entries = (values,) if isinstance(values, str) else values
            if not isinstance(entries, tuple) or not all(isinstance(item, str) for item in entries):
                raise TypeError(
                    'appendix_system_prompt values must be a string or tuple of strings'
                )


_WEB_SEARCH_ENGINE_INSTANCES: list = [
    GoogleSearch(),
    BingSearch(),
    BochaSearch(),
    TavilySearch(),
]

_ACADEMIC_SEARCH_ENGINE_INSTANCES: list = [
    SciverseSearch(),
    ArxivSearch(skip_auth=True),
]


class WikipediaToolkit(WikipediaSearch):
    """Search stable encyclopedic background and named entries in Wikipedia.

    Use this for established concepts, people, places, organizations, and historical topics.
    It is not a general web search engine and should not be used for current events, recent product
    information, recommendations, industry developments, or broad open-web research.
    """


def _temp_kb_key_source() -> Any:
    agentic_config = lazyllm.globals.get('agentic_config') or {}
    return agentic_config.get('files')


def _kb_prompt_appendix() -> SystemPromptAppendix:
    appendix: SystemPromptAppendix = {
        'output_contract': (
            *IMAGE_MARKDOWN_OUTPUT_APPENDIX['output_contract'],
            *KNOWLEDGE_CITATION_OUTPUT_APPENDIX['output_contract'],
        ),
    }
    agentic_config = lazyllm.globals.get('agentic_config') or {}
    if (agentic_config.get('filters') or {}).get('kb_id'):
        appendix['tool_policy'] = KNOWLEDGE_SEARCH_TOOL_POLICY_APPENDIX['tool_policy']
    return appendix


SKILL_TOOL_GROUP = ToolGroupConfig(
    name='skill',
    label='技能工具',
    description='利用已安装的技能进行查询、读文件、执行脚本',
    instance=None,
    label_en='Skills',
    description_en='Use installed skills to search, read files, and run scripts.',
)

DEFAULT_TOOLS: list[ToolGroupConfig] = [
    ToolGroupConfig(
        name='kb',
        label='知识库检索',
        description='从知识库中搜索文档，支持语义检索、关键词检索、上下文窗口等',
        instance=KBToolGroup(),
        label_en='Knowledge Base Search',
        description_en='Search knowledge base documents using semantic search, keyword search, and context windows.',
        capability_id='knowledge_base_search',
        input_schema={'query': 'string'}, output_schema={'results': 'list'}, required_config=['knowledge_base'],
        appendix_system_prompt=_kb_prompt_appendix,
    ),
    ToolGroupConfig(
        name='temp_kb',
        label='临时文件检索',
        description='从用户上传的临时文件中搜索相关内容',
        instance=kb_tmp_search,
        label_en='Temporary File Search',
        description_en='Search relevant content in temporary files uploaded by the user.',
        key_source=_temp_kb_key_source,
        appendix_system_prompt={
            'output_contract': KNOWLEDGE_CITATION_OUTPUT_APPENDIX['output_contract'],
        },
    ),
    ToolGroupConfig(
        name='system_query',
        label='系统数据查询',
        description='只读查询 LazyMind 知识库、文档、数据源和关联统计',
        instance=SystemQueryToolGroup(),
        label_en='System Data Query',
        description_en=(
            'Read-only queries for LazyMind knowledge bases, documents, data sources, and related statistics.'
        ),
    ),
    ToolGroupConfig(
        name='external_db',
        label='外部数据库查询',
        description='只读查看已配置外部数据库 schema，并执行只读 SELECT/WITH 查询',
        instance=ExternalDBToolGroup(),
        label_en='External Database Query',
        description_en='Inspect configured external database schemas and run read-only SELECT or WITH queries.',
    ),
    ToolGroupConfig(
        name='writer',
        label='AI 写作',
        description='构建写作任务、资料画像、写作上下文、大纲、章节草稿、审阅报告和最终成稿',
        instance=WriterToolGroup(),
        label_en='AI Writing',
        description_en=(
            'Build writing tasks, source profiles, context, outlines, chapter drafts, review reports, and final drafts.'
        ),
    ),
    ToolGroupConfig(
        name='calculator',
        label='科学计算器',
        description='安全地执行数学表达式计算',
        instance=calculator,
        label_en='Scientific Calculator',
        description_en='Safely evaluate mathematical expressions.',
    ),
    ToolGroupConfig(
        name='wikipedia',
        label='Wikipedia 搜索',
        description='查询 Wikipedia 中稳定的百科背景和明确词条；不用于新闻、时效信息或开放网页搜索',
        instance=WikipediaToolkit(skip_auth=True),
        label_en='Wikipedia Search',
        description_en=(
            'Look up stable encyclopedic background and named Wikipedia entries; not for news, '
            'current information, or open-web search.'
        ),
    ),
    ToolGroupConfig(
        name='web_search',
        label='网页搜索',
        description='使用搜索引擎检索互联网内容，自动选择可用的搜索服务',
        instance=_WEB_SEARCH_ENGINE_INSTANCES,
        label_en='Web Search',
        description_en=(
            'Search the open internet for current information and broad research using the first '
            'available provider.'
        ),
        pick_first_valid=True,
        capability_id='web_search',
        equivalence_scope='provider_bound',
        input_schema={'query': 'string'}, output_schema={'results': 'list'}, required_config=['search_provider'],
        appendix_system_prompt=WEB_SEARCH_TOOL_POLICY_APPENDIX,
    ),
    ToolGroupConfig(
        name='academic_search',
        label='学术搜索',
        description='搜索学术论文和科学文献，自动选择可用的学术搜索服务',
        instance=_ACADEMIC_SEARCH_ENGINE_INSTANCES,
        label_en='Academic Search',
        description_en='Search academic papers and scientific literature using the first available provider.',
        pick_first_valid=True,
        capability_id='academic_search',
        equivalence_scope='provider_bound',
        input_schema={'query': 'string'}, output_schema={'papers': 'list'}, required_config=['academic_search_provider'],
    ),
    ToolGroupConfig(
        name='url_fetch',
        label='网页抓取',
        description='获取并解析公开网页的可读内容',
        instance=url_fetch,
        label_en='Web Page Fetch',
        description_en='Fetch and parse readable content from public web pages.',
    ),
    ToolGroupConfig(
        name='multimodal',
        label='多模态识别',
        description='从图片中提取文字描述',
        instance=vision_extractor,
        label_en='Multimodal Recognition',
        description_en='Extract text descriptions from images.',
        model_role='vlm',
    ),
    ToolGroupConfig(
        name='image_generator',
        label='文生图',
        description='根据文字描述生成图片',
        instance=image_generator,
        label_en='Image Generation',
        description_en='Generate images from text descriptions.',
        model_role='image_generator',
        capability_id='image_generation',
        input_schema={'prompt': 'string'}, output_schema={'image': 'file'}, required_config=['image_generator_model'],
        appendix_system_prompt=IMAGE_MARKDOWN_OUTPUT_APPENDIX,
    ),
    ToolGroupConfig(
        name='image_editor',
        label='图编辑',
        description='根据文字指令编辑参考图片',
        instance=image_editor,
        label_en='Image Editing',
        description_en='Edit reference images using text instructions.',
        model_role='image_editor',
        capability_id='image_editing',
        appendix_system_prompt=IMAGE_MARKDOWN_OUTPUT_APPENDIX,
    ),
    ToolGroupConfig(
        name='video_generator',
        label='文生视频',
        description='根据文字描述生成视频，可选首帧参考图；同轮多次调用并行，视频侧最多同时3路',
        instance=video_generator,
        label_en='Video Generation',
        description_en='Generate videos from text, optionally using a first-frame reference image.',
        model_role='video_generator',
        capability_id='video_generation',
        input_schema={'prompt': 'string'}, output_schema={'video': 'file'},
        required_config=['video_generator_model'],
        appendix_system_prompt=VIDEO_MARKDOWN_OUTPUT_APPENDIX,
    ),
    ToolGroupConfig(
        name='video_to_gif',
        label='视频转GIF',
        description='将本地视频转换为 GIF 动图；同轮多次调用并行，GIF 侧最多同时3路',
        instance=video_to_gif,
        label_en='Video to GIF',
        description_en='Convert a local video into an animated GIF.',
        capability_id='video_to_gif',
        input_schema={'url': 'string'}, output_schema={'image': 'file'},
        appendix_system_prompt=IMAGE_MARKDOWN_OUTPUT_APPENDIX,
    ),
    ToolGroupConfig(
        name='vocab_learn',
        label='词汇学习',
        description='学习用户专属的词汇映射和同义词',
        instance=vocab_learn,
        label_en='Vocabulary Learning',
        description_en='Learn user-specific vocabulary mappings and synonyms.',
    ),
    ToolGroupConfig(
        name='read_memory',
        label='记忆读取',
        description='读取当前的用户记忆或偏好内容',
        instance=read_memory,
        label_en='Memory Reading',
        description_en='Read the current user memory and preferences.',
        appendix_system_prompt=MEMORY_READER_TOOL_POLICY_APPENDIX,
    ),
    ToolGroupConfig(
        name='memory_editor',
        label='记忆编辑',
        description='记录和编辑跨会话的用户记忆和偏好',
        instance=memory_editor,
        label_en='Memory Editing',
        description_en='Record and edit user memories and preferences across conversations.',
    ),
    ToolGroupConfig(
        name='skill_editor',
        label='技能编辑',
        description='创建、修改和删除技能',
        instance=SkillEditorToolGroup(),
        label_en='Skill Editing',
        description_en='Create, update, and delete skills.',
    ),
    ToolGroupConfig(
        name='local_fs',
        label='本地文件',
        description='在配置的本地路径内进行 glob 匹配、grep 搜索、文件读取（只读）',
        instance=LocalFSToolGroup(),
        label_en='Local Files',
        description_en='Run glob matching, grep searches, and read-only file access within configured local paths.',
    ),
    ToolGroupConfig(
        name='feishu',
        label='飞书文件系统',
        description='浏览和管理飞书云文档',
        instance=FeishuWikiFS(space_id='dynamic', dynamic_auth=True),
        label_en='Feishu File System',
        description_en='Browse and manage Feishu cloud documents.',
    ),
    ToolGroupConfig(
        name='notion',
        label='Notion 文件系统',
        description='浏览、搜索和管理 Notion 页面',
        instance=NotionFS(dynamic_auth=True),
        label_en='Notion File System',
        description_en='Browse, search, and manage Notion pages.',
    ),
    ToolGroupConfig(
        name='google_drive',
        label='Google Drive 文件系统',
        description='搜索和读取 Google Drive 与 Google Workspace 文档',
        instance=GoogleDriveFS(dynamic_auth=True),
        label_en='Google Drive File System',
        description_en='Search and read Google Drive and Google Workspace documents.',
    ),
]


def _resolve_method_name(instance: Any, method_name: str) -> str:
    if method_name == '__call__':
        return instance.__class__.__name__
    return method_name


def _extract_methods(instance: Any) -> list[dict]:
    public_apis = getattr(instance, '__public_apis__', None)
    if public_apis is not None:
        methods = []
        for method_name in public_apis:
            resolved_name = _resolve_method_name(instance, method_name)
            method = getattr(instance, method_name, None)
            if method is None:
                methods.append({'name': resolved_name, 'summary': ''})
                continue
            try:
                doc = inspect.getdoc(method)
                summary = docstring_parser.parse(doc).short_description if doc else ''
            except Exception:
                summary = ''
            methods.append({'name': resolved_name, 'summary': summary})
        return methods

    if callable(instance):
        name = getattr(instance, '__name__', '')
        try:
            doc = inspect.getdoc(instance)
            summary = docstring_parser.parse(doc).short_description if doc else ''
        except Exception:
            summary = ''
        return [{'name': name, 'summary': summary}]

    return []


def _extract_group_methods(instances: list) -> list[dict]:
    methods = []
    for inst in instances:
        name = inst.__class__.__name__
        try:
            doc = inspect.getdoc(inst)
            summary = docstring_parser.parse(doc).short_description if doc else ''
        except Exception:
            summary = ''
        methods.append({
            'name': name,
            'summary': summary,
            'active': _instance_is_active(inst),
        })
    return methods


_SKILL_METHODS = [
    {'name': 'get_skill', 'summary': 'Get the full usage for a skill (SKILL.md).'},
    {'name': 'read_reference', 'summary': 'Read a reference file within a skill directory.'},
    {'name': 'run_script', 'summary': 'Run a script within a skill directory.'},
]


def _instance_is_active(instance: Any) -> bool:
    key_source = getattr(instance, '__key_source__', None)
    if key_source is None:
        return True
    return _key_source_is_active(key_source)


def _key_source_is_active(key_source: Callable[[], Any]) -> bool:
    try:
        return bool(key_source())
    except Exception:
        return False


def group_is_active(cfg: ToolGroupConfig) -> bool:
    if cfg.model_role and not is_model_role_available(cfg.model_role):
        return False
    if cfg.key_source and not _key_source_is_active(cfg.key_source):
        return False
    if cfg.pick_first_valid:
        return any(_instance_is_active(inst) for inst in cfg.instance)
    if cfg.instance is None:
        return True
    result = _instance_is_active(cfg.instance)
    if cfg.name == 'kb':
        from lazyllm import LOG as _LOG
        _LOG.info(f'[KBToolGroup_DEBUG] group_is_active kb={result!r}')
    return result


def normalize_tool_locale(locale: str | None) -> str:
    for part in (locale or '').split(','):
        tag = part.split(';', 1)[0].strip().lower()
        if tag == 'zh' or tag.startswith('zh-'):
            return 'zh-CN'
        if tag == 'en' or tag.startswith('en-'):
            return 'en-US'
    return 'zh-CN'


def get_all_tool_groups(locale: str | None = None) -> list[dict]:
    use_english = normalize_tool_locale(locale) == 'en-US'
    result = []
    for cfg in DEFAULT_TOOLS:
        if cfg.pick_first_valid:
            methods = _extract_group_methods(cfg.instance)
        else:
            methods = _extract_methods(cfg.instance)
        result.append({
            'name': cfg.name,
            'label': cfg.label_en or cfg.label if use_english else cfg.label,
            'description': cfg.description_en or cfg.description if use_english else cfg.description,
            'methods': methods,
            'can_disable': True,
            'active': group_is_active(cfg),
            'capability_id': cfg.capability_id or cfg.name,
            'equivalence_scope': cfg.equivalence_scope,
            'provider_id': cfg.provider_id,
            'product_id': cfg.product_id,
            'input_schema': cfg.input_schema or {},
            'output_schema': cfg.output_schema or {},
            'required_config': cfg.required_config or [],
        })
    result.append({
        'name': SKILL_TOOL_GROUP.name,
        'label': SKILL_TOOL_GROUP.label_en or SKILL_TOOL_GROUP.label if use_english else SKILL_TOOL_GROUP.label,
        'description': (
            SKILL_TOOL_GROUP.description_en or SKILL_TOOL_GROUP.description
            if use_english else SKILL_TOOL_GROUP.description
        ),
        'methods': _SKILL_METHODS,
        'can_disable': False,
        'active': True,
    })
    return result


_CAPABILITY_DENY_CUES = re.compile(
    r'不要(?:使用|调用|查询|检索|搜索|启用|用)?|别(?:再)?(?:使用|调用|查询|检索|搜索|用)|'
    r'不想(?:使用|调用|用)|不(?:使用|用)|无需|不能(?:用|使用)|'
    r'禁止(?:使用|调用)|避免使用|排除|忽略|跳过|do\s+not\s+use|'
    r'don[’\']t\s+use|never\s+use|without|exclude|ignore|avoid', re.I,
)
_CAPABILITY_ALLOW_CUES = re.compile(
    r'可以(?:用|使用)|可(?:用|使用)|请(?:用|使用)|优先使用|允许使用|使用|调用|启用|'
    r'can\s+use|may\s+use|please\s+use|use|enable', re.I,
)
_TOOL_CAPABILITY_TERMS: dict[str, tuple[str, ...]] = {
    'kb': ('知识库', 'knowledge base'),
}


def _capability_is_denied(query: str, terms: tuple[str, ...]) -> bool:
    """Return true only when every locally qualified occurrence is denied."""
    decisions = []
    lowered = query.lower()
    for term in terms:
        for match in re.finditer(re.escape(term.lower()), lowered):
            prefix = query[max(0, match.start() - 40):match.start()]
            prefix = re.split(r'[，,。；;！？!?\n]|但是|不过|然而|但', prefix)[-1]
            denies = list(_CAPABILITY_DENY_CUES.finditer(prefix))
            allows = list(_CAPABILITY_ALLOW_CUES.finditer(prefix))
            if denies or allows:
                decisions.append(
                    bool(denies) and (not allows or denies[-1].end() >= allows[-1].end())
                )
    return bool(decisions) and all(decisions)


def filter_tools(
    configs: list[ToolGroupConfig],
    available_tools: list[str] | None = None,
    user_query: str = '',
) -> list[ToolGroupConfig]:
    result = []
    for cfg in configs:
        if available_tools is not None and cfg.name not in available_tools:
            continue
        terms = _TOOL_CAPABILITY_TERMS.get(cfg.name)
        if terms and user_query and _capability_is_denied(user_query, terms):
            continue
        if not group_is_active(cfg):
            continue
        result.append(cfg)
    return result


def build_agent_tools(configs: list[ToolGroupConfig]) -> list:
    result = []
    for cfg in configs:
        if cfg.pick_first_valid:
            group = dict(
                name=cfg.name,
                desc=cfg.description,
                pick_first_valid=True,
                tools=list(cfg.instance),
            )
            if cfg.key_source:
                group['key_source'] = cfg.key_source
            result.append(group)
        elif cfg.key_source:
            result.append((cfg.instance, cfg.key_source))
        else:
            result.append(cfg.instance)
    return result


def collect_system_prompt_appendices(
    configs: list[ToolGroupConfig],
    extra_appendices: tuple[SystemPromptAppendix, ...] = (),
) -> dict[str, list[str]]:
    """Collect active tool prompt appendices with stable per-section deduplication."""
    collected: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    appendices = []
    for cfg in configs:
        provider = cfg.appendix_system_prompt
        appendix = provider() if callable(provider) else provider
        if appendix:
            ToolGroupConfig._validate_appendix(appendix)
            appendices.append(appendix)
    appendices.extend(extra_appendices)
    for appendix in appendices:
        for section, values in appendix.items():
            entries = (values,) if isinstance(values, str) else values
            for content in entries:
                original = content.strip()
                if not original:
                    continue
                dedupe_key = ' '.join(original.split())
                section_seen = seen.setdefault(section, set())
                if dedupe_key in section_seen:
                    continue
                section_seen.add(dedupe_key)
                collected.setdefault(section, []).append(original)
    return collected
