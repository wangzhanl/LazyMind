import importlib

import pytest

from lazymind.chat.engine.tools import kb
from lazyllm.tools.agent.toolsManager import MethodModuleTool

skill_editor_mod = importlib.import_module('lazymind.chat.engine.tools.skill_editor')
skill_operations_mod = importlib.import_module('lazymind.chat.engine.tools.infra.skill_operations')


PENDING_SKILL_CHANGE_MESSAGE = (
    'There are pending changes. Please ask the user to handle them before modifying.'
)


def test_kb_keyword_search_requires_explicit_target_in_schema():
    tool = MethodModuleTool(kb.KBToolGroup(), 'kb_keyword_search')

    required = set(tool.params_schema.model_json_schema().get('required', []))

    assert {'keyword', 'target'}.issubset(required)
    assert 'target_type' not in required
    assert 'docid' not in tool.params_schema.model_fields
    assert 'file_name' not in tool.params_schema.model_fields


def test_kb_keyword_search_maps_target_by_type(monkeypatch):
    calls = []

    class FakeDocument:
        def keyword_search(self, **kwargs):
            calls.append(kwargs)
            return []

    monkeypatch.setattr(kb.lazyllm, 'globals', {'agentic_config': {'filters': {'kb_id': 'kb-1'}}})
    monkeypatch.setattr(kb, 'DOCUMENT', FakeDocument())

    by_file = kb.KBToolGroup().kb_keyword_search('DeepSeek', 'report.pdf')
    by_docid = kb.KBToolGroup().kb_keyword_search('DeepSeek', 'doc-1', target_type='docid')

    assert by_file['success'] is True
    assert calls[0]['file_name'] == 'report.pdf'
    assert calls[0]['doc_id'] == ''
    assert by_docid['success'] is True
    assert calls[1]['file_name'] is None
    assert calls[1]['doc_id'] == 'doc-1'


def test_skill_editor_returns_error_result_for_skill_file_exception(monkeypatch):
    def raise_unexpected(*args, **kwargs):
        raise RuntimeError('skill files unavailable')

    monkeypatch.setattr(skill_editor_mod.lazyllm, 'globals', {'agentic_config': {}})
    monkeypatch.setattr(skill_operations_mod, '_list_skill_files', raise_unexpected)

    result = skill_editor_mod.SkillEditorToolGroup().edit_file(
        'existing',
        'coding',
        path='SKILL.md',
        content='updated content',
    )

    assert result['success'] is False
    assert result['tool'] == 'edit_file'
    assert result['error']['reason'] == 'Failed to load or edit skill package: skill files unavailable'


@pytest.mark.parametrize(
    ('method_name', 'kwargs'),
    [
        ('edit_file', {'path': 'SKILL.md', 'content': 'updated content'}),
        ('patch_file', {'path': 'SKILL.md', 'old_text': 'old', 'new_text': 'new'}),
        ('create_file', {'path': 'references/new.md', 'content': 'new reference'}),
        ('delete_file', {'path': 'references/old.md'}),
    ],
)
def test_skill_editor_maps_pending_draft_error_for_file_operations(monkeypatch, method_name, kwargs):
    def raise_pending_draft(*args, **kwargs):
        raise RuntimeError('draft belongs to another task')

    monkeypatch.setattr(skill_editor_mod.lazyllm, 'globals', {'agentic_config': {}})
    monkeypatch.setattr(skill_operations_mod, '_list_skill_files', raise_pending_draft)

    result = getattr(skill_editor_mod.SkillEditorToolGroup(), method_name)(
        'existing',
        'coding',
        **kwargs,
    )

    assert result['success'] is False
    assert result['tool'] == method_name
    assert result['error']['reason'] == PENDING_SKILL_CHANGE_MESSAGE


@pytest.mark.parametrize('method_name', ['create_skill', 'rename_skill', 'remove_skill'])
def test_skill_editor_maps_pending_draft_error_for_package_operations(monkeypatch, method_name):
    def raise_pending_draft(*args, **kwargs):
        raise RuntimeError('draft belongs to another task')

    content = (
        '---\n'
        'name: existing\n'
        'category: coding\n'
        'description: Existing skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    monkeypatch.setattr(skill_editor_mod.lazyllm, 'globals', {'agentic_config': {}})
    monkeypatch.setattr(skill_editor_mod, 'create_remote_skill', raise_pending_draft)
    monkeypatch.setattr(skill_editor_mod, 'list_skill_files', raise_pending_draft)
    monkeypatch.setattr(skill_editor_mod, 'remove_remote_skill', raise_pending_draft)

    if method_name == 'create_skill':
        result = skill_editor_mod.SkillEditorToolGroup().create_skill('existing', 'coding', content)
    elif method_name == 'rename_skill':
        result = skill_editor_mod.SkillEditorToolGroup().rename_skill('existing', 'coding', 'renamed')
    else:
        result = skill_editor_mod.SkillEditorToolGroup().remove_skill('existing', 'coding')

    assert result['success'] is False
    assert result['tool'] == method_name
    assert result['error']['reason'] == PENDING_SKILL_CHANGE_MESSAGE


def test_skill_editor_tool_group_exposes_action_specific_schemas():
    group = skill_editor_mod.SkillEditorToolGroup()
    create_tool = MethodModuleTool(group, 'create_skill')
    patch_tool = MethodModuleTool(group, 'patch_file')

    assert create_tool.name == 'SkillEditorToolGroup_create_skill'
    assert patch_tool.name == 'SkillEditorToolGroup_patch_file'
    assert 'modify_skill' not in group.__public_apis__
    create_fields = set(create_tool.params_schema.model_fields)
    patch_fields = set(patch_tool.params_schema.model_fields)
    patch_required = set(patch_tool.params_schema.model_json_schema().get('required', []))

    assert create_fields == {'name', 'category', 'content'}
    assert patch_fields == {'name', 'category', 'path', 'old_text', 'new_text', 'replace_all', 'reason'}
    assert {'name', 'category', 'path', 'old_text', 'new_text'}.issubset(patch_required)
