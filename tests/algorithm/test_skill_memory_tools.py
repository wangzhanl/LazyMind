import importlib

memory_mod = importlib.import_module('lazymind.chat.engine.tools.memory_editor')
skill_editor_mod = importlib.import_module('lazymind.chat.engine.tools.skill_editor')


def test_memory_editor_operations_write_memory_review(monkeypatch):
    assert not hasattr(memory_mod, 'memory')

    class FakeUnprocessableContentError(ValueError):
        pass

    records = []

    def fake_insert_memory_review_record(**kwargs):
        records.append(kwargs)
        return {'id': 'review-1', 'review_status': 'pending'}

    monkeypatch.setattr(memory_mod, 'UnprocessableContentError', FakeUnprocessableContentError)
    monkeypatch.setattr(
        memory_mod,
        '_validate_generated_content',
        lambda memory_type, content: content,
    )
    monkeypatch.setattr(
        memory_mod,
        '_apply_memory_edit_operations',
        lambda current, payload: current.replace('old', payload['operations'][0]['new']),
    )
    monkeypatch.setattr(
        memory_mod,
        '_apply_user_preference_edit_operations',
        lambda current, payload: current.replace('old', payload['operations'][0]['new']),
    )
    monkeypatch.setattr(memory_mod, 'insert_memory_review_record', fake_insert_memory_review_record)
    monkeypatch.setattr(
        memory_mod.lazyllm,
        'globals',
        {'agentic_config': {'user_id': 'user-1', 'memory': 'old', 'user_preference': 'old'}},
    )

    memory_result = memory_mod.memory_editor(
        'memory',
        [{'op': 'replace_text', 'old': 'old', 'new': 'new'}],
    )
    user_result = memory_mod.memory_editor(
        'user_preference',
        [{'op': 'replace_text', 'old': 'old', 'new': 'new'}],
    )

    assert memory_result['success'] is True
    assert memory_result['tool'] == 'memory_editor'
    assert memory_result['result']['target'] == 'memory'
    assert memory_result['result']['status'] == 'pending_review'
    assert memory_result['result']['message'] == '记忆修改已提交，等待审核'
    assert user_result['success'] is True
    assert user_result['tool'] == 'memory_editor'
    assert user_result['result']['target'] == 'user_preference'
    assert user_result['result']['status'] == 'pending_review'
    assert user_result['result']['message'] == '记忆修改已提交，等待审核'
    assert records == [
        {
            'target': 'memory',
            'user_id': 'user-1',
            'session_id': '',
            'source_content': 'old',
            'content': 'new',
            'operations': [{'op': 'replace_text', 'old': 'old', 'new': 'new'}],
        },
        {
            'target': 'user_preference',
            'user_id': 'user-1',
            'session_id': '',
            'source_content': 'old',
            'content': 'new',
            'operations': [{'op': 'replace_text', 'old': 'old', 'new': 'new'}],
        },
    ]


def test_skill_editor_create_modify_remove_core_paths(monkeypatch):
    create_calls = []
    remove_calls = []
    replace_calls = []

    monkeypatch.setattr(
        skill_editor_mod.lazyllm,
        'globals',
        {'agentic_config': {'user_id': 'user-1', 'session_id': 'session-1'}},
    )
    monkeypatch.setattr(skill_editor_mod, 'create_remote_skill', lambda *args: create_calls.append(args))
    monkeypatch.setattr(skill_editor_mod, 'remove_remote_skill', lambda *args: remove_calls.append(args))

    existing_content = (
        '---\n'
        'name: existing\n'
        'category: writing\n'
        'description: Existing skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    monkeypatch.setattr(
        skill_editor_mod,
        'list_all_skill_entries',
        lambda _base_dir: {
            'writing/existing': {
                'name': 'existing',
                'category': 'writing',
                'path': '/tmp/skills/writing/existing',
                'source': 'remote',
                'content': existing_content,
            }
        },
    )
    monkeypatch.setattr(
        skill_editor_mod,
        'list_skill_files',
        lambda category, name: {
            'SKILL.md': existing_content,
            'references/old.md': 'old reference\n',
        },
    )

    def fake_replace_skill_package_files(category, name, before, after):
        replace_calls.append((category, name, before, after))
        return {
            'written': sorted(path for path in after if before.get(path) != after.get(path)),
            'deleted': sorted(set(before) - set(after)),
        }

    monkeypatch.setattr(skill_editor_mod, 'replace_skill_package_files', fake_replace_skill_package_files)

    content = (
        '---\n'
        'name: new_skill\n'
        'category: drafts\n'
        'description: A test skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    create_result = skill_editor_mod.skill_editor(
        'new_skill',
        'create',
        category='drafts',
        content=content,
    )
    modify_result = skill_editor_mod.skill_editor(
        'existing',
        'modify',
        category='writing',
        operations=[
            {
                'op': 'patch_file',
                'path': 'SKILL.md',
                'old_text': 'Use this skill for tests.',
                'new_text': 'Use this skill for focused tests.',
            },
            {
                'op': 'write_file',
                'path': 'scripts/check.py',
                'content': 'print("ok")\n',
            },
            {'op': 'delete_file', 'path': 'references/old.md'},
        ],
        reason='package update',
    )
    remove_result = skill_editor_mod.skill_editor('existing', 'remove', category='writing')

    assert create_result['success'] is True
    assert create_result['tool'] == 'skill_editor'
    assert modify_result['success'] is True
    assert modify_result['tool'] == 'skill_editor'
    assert remove_result['success'] is True
    assert remove_result['tool'] == 'skill_editor'
    assert create_result['result'] == {
        'status': 'created',
        'message': 'Skill was created and is now active.',
    }
    assert modify_result['result']['status'] == 'modified'
    assert modify_result['result']['message'] == 'Skill package was modified and is now active.'
    assert modify_result['result']['touched_files'] == ['SKILL.md', 'references/old.md', 'scripts/check.py']
    assert modify_result['result']['written_files'] == ['SKILL.md', 'scripts/check.py']
    assert modify_result['result']['deleted_files'] == ['references/old.md']
    assert modify_result['result']['summary'] == 'package update'
    assert remove_result['result'] == {
        'status': 'removed',
        'message': 'Skill was removed and is no longer active.',
    }
    assert create_calls == [('drafts', 'new_skill', content)]
    assert remove_calls == [('writing', 'existing')]
    assert replace_calls == [
        (
            'writing',
            'existing',
            {'SKILL.md': existing_content, 'references/old.md': 'old reference\n'},
            {
                'SKILL.md': existing_content.replace(
                    'Use this skill for tests.',
                    'Use this skill for focused tests.',
                ),
                'scripts/check.py': 'print("ok")\n',
            },
        ),
    ]


def test_skill_editor_rejects_missing_skill_without_write(monkeypatch):
    calls = []

    monkeypatch.setattr(skill_editor_mod.lazyllm, 'globals', {'agentic_config': {}})
    monkeypatch.setattr(skill_editor_mod, 'replace_skill_package_files', lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(skill_editor_mod, 'list_all_skill_entries', lambda _base_dir: {})

    result = skill_editor_mod.skill_editor(
        'missing',
        'modify',
        category='writing',
        operations=[{'op': 'patch_file', 'old_text': 'old', 'new_text': 'new'}],
    )

    assert result == {
        'success': False,
        'tool': 'skill_editor',
        'error': {
            'reason': "Skill 'missing' does not exist in category 'writing'; use action='create' to add a new skill.",
        },
    }
    assert calls == []


def test_skill_editor_renames_package_and_updates_runtime_delta(monkeypatch):
    rename_calls = []
    existing_content = (
        '---\n'
        'name: existing\n'
        'category: writing\n'
        'description: Existing skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    monkeypatch.setattr(skill_editor_mod.lazyllm, 'globals', {'agentic_config': {'user_id': 'user-1'}})
    monkeypatch.setattr(
        skill_editor_mod,
        'list_all_skill_entries',
        lambda _base_dir: {
            'writing/existing': {
                'name': 'existing',
                'category': 'writing',
                'path': '/tmp/skills/writing/existing',
                'source': 'remote',
                'content': existing_content,
            }
        },
    )
    monkeypatch.setattr(
        skill_editor_mod,
        'list_skill_files',
        lambda category, name: {'SKILL.md': existing_content, 'references/doc.md': 'doc\n'},
    )
    monkeypatch.setattr(skill_editor_mod, 'rename_skill_package', lambda *args, **kwargs: rename_calls.append((args, kwargs)))

    result = skill_editor_mod.skill_editor(
        'existing',
        'rename',
        category='writing',
        new_name='renamed',
        new_category='drafts',
    )

    assert result['success'] is True
    assert result['result']['status'] == 'renamed'
    assert result['result']['old'] == {'category': 'writing', 'name': 'existing'}
    assert result['result']['new'] == {'category': 'drafts', 'name': 'renamed'}
    assert rename_calls[0][0][:4] == ('writing', 'existing', 'drafts', 'renamed')
    assert 'name: renamed' in rename_calls[0][1]['skill_content']
    assert 'category: drafts' in rename_calls[0][1]['skill_content']
    assert skill_editor_mod.lazyllm.globals['agentic_config']['skill_runtime_delta']['renamed'] == [
        {
            'old': {'category': 'writing', 'name': 'existing'},
            'new': {'category': 'drafts', 'name': 'renamed'},
        }
    ]
