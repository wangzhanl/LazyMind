import importlib
import io

memory_mod = importlib.import_module('lazymind.chat.engine.tools.memory_editor')
memory_reader_mod = importlib.import_module('lazymind.chat.engine.tools.memory_reader')
skill_editor_mod = importlib.import_module('lazymind.chat.engine.tools.skill_editor')
skill_operations_mod = importlib.import_module('lazymind.chat.engine.tools.infra.skill_operations')
skill_remote_store_mod = importlib.import_module('lazymind.chat.engine.tools.infra.skill_remote_store')


class FakeSkillRemoteFS:
    _fs_protocol_key = 'remote'

    def __init__(self, packages):
        self.packages = packages

    def ls(self, path, detail=True):
        normalized = skill_editor_mod.RemoteFS._normalize_path(path)
        parts = [part for part in normalized.split('/') if part]
        if parts == ['skills']:
            categories = sorted({category for category, _name in self.packages})
            entries = [
                {'name': f'remote://skills/{category}', 'path': f'skills/{category}', 'type': 'directory'}
                for category in categories
            ]
        elif len(parts) == 2 and parts[0] == 'skills':
            skills = sorted(name for category, name in self.packages if category == parts[1])
            entries = [
                {
                    'name': f'remote://skills/{parts[1]}/{name}',
                    'path': f'skills/{parts[1]}/{name}',
                    'type': 'directory',
                }
                for name in skills
            ]
        elif len(parts) == 3 and parts[0] == 'skills' and (parts[1], parts[2]) in self.packages:
            entries = [
                {
                    'name': f'remote://skills/{parts[1]}/{parts[2]}/SKILL.md',
                    'path': f'skills/{parts[1]}/{parts[2]}/SKILL.md',
                    'type': 'file',
                }
            ]
        else:
            entries = []
        return entries if detail else [entry['name'] for entry in entries]

    def info(self, path):
        normalized = skill_editor_mod.RemoteFS._normalize_path(path)
        parts = [part for part in normalized.split('/') if part]
        content = self.packages.get((parts[1], parts[2]), '') if len(parts) >= 4 else ''
        return {'size': len(content.encode('utf-8'))}

    def open(self, path, *args, **kwargs):
        normalized = skill_editor_mod.RemoteFS._normalize_path(path)
        parts = [part for part in normalized.split('/') if part]
        return io.StringIO(self.packages[(parts[1], parts[2])])


class FakeMemoryStore:
    def __init__(self, contents=None, read_error=None, write_error=None):
        self.contents = dict(contents or {})
        self.read_error = read_error
        self.write_error = write_error
        self.writes = []

    def read(self, target):
        if self.read_error:
            raise self.read_error
        return self.contents[target]

    def write(self, target, content):
        if self.write_error:
            raise self.write_error
        self.writes.append((target, content))
        self.contents[target] = content


def test_memory_editor_operation_writes_remote_fs(monkeypatch):
    assert not hasattr(memory_mod, 'memory')
    assert not hasattr(memory_mod, 'insert_memory_review_record')

    store = FakeMemoryStore({'memory': 'old', 'user_preference': 'old'})
    monkeypatch.setattr(
        memory_mod,
        '_validate_generated_content',
        lambda memory_type, content: content,
    )
    monkeypatch.setattr(memory_mod, 'MemoryRemoteStore', lambda: store)

    memory_result = memory_mod.memory_editor(
        'memory',
        op='patch',
        old_text='old',
        new_text='new',
    )
    user_result = memory_mod.memory_editor(
        'user_preference',
        op='append',
        content='newer',
    )

    assert memory_result['success'] is True
    assert memory_result['tool'] == 'memory_editor'
    assert memory_result['result']['target'] == 'memory'
    assert memory_result['result']['status'] == 'pending_review'
    assert memory_result['result']['message'] == 'Memory changes were written to draft and are pending review.'
    assert memory_result['result']['operation_count'] == 1
    assert user_result['success'] is True
    assert user_result['tool'] == 'memory_editor'
    assert user_result['result']['target'] == 'user_preference'
    assert user_result['result']['status'] == 'pending_review'
    assert user_result['result']['message'] == 'Memory changes were written to draft and are pending review.'
    assert store.writes == [
        ('memory', 'new'),
        ('user_preference', 'old\nnewer'),
    ]


def test_memory_editor_patch_match_controls(monkeypatch):
    store = FakeMemoryStore({'memory': 'old and old'})
    monkeypatch.setattr(memory_mod, 'MemoryRemoteStore', lambda: store)
    monkeypatch.setattr(memory_mod, '_validate_generated_content', lambda memory_type, content: content)

    ambiguous = memory_mod.memory_editor(
        'memory',
        op='patch',
        old_text='old',
        new_text='new',
    )
    missing = memory_mod.memory_editor(
        'memory',
        op='patch',
        old_text='missing',
        new_text='new',
    )
    empty_old = memory_mod.memory_editor(
        'memory',
        op='patch',
        old_text='',
        new_text='new',
    )
    replace_all = memory_mod.memory_editor(
        'memory',
        op='patch',
        old_text='old',
        new_text='new',
        replace_all_matches=True,
    )

    assert ambiguous['success'] is False
    assert 'matched multiple locations' in ambiguous['error']['reason']
    assert missing['success'] is False
    assert 'could not find' in missing['error']['reason']
    assert empty_old['success'] is False
    assert "non-empty 'old_text'" in empty_old['error']['reason']
    assert replace_all['success'] is True
    assert store.writes == [('memory', 'new and new')]


def test_memory_editor_append_requires_content(monkeypatch):
    store = FakeMemoryStore({'memory': ''})
    monkeypatch.setattr(memory_mod, 'MemoryRemoteStore', lambda: store)

    empty_append = memory_mod.memory_editor('memory', op='append', content='  ')
    unknown = memory_mod.memory_editor('memory', op='replace_all', content='new')

    assert empty_append['success'] is False
    assert 'append requires non-empty content' in empty_append['error']['reason']
    assert unknown['success'] is False
    assert "expected 'patch' or 'append'" in unknown['error']['reason']
    assert store.writes == []


def test_memory_editor_remote_fs_errors_return_tool_error(monkeypatch):
    monkeypatch.setattr(
        memory_mod,
        'MemoryRemoteStore',
        lambda: FakeMemoryStore({'memory': 'old'}, read_error=RuntimeError('backend down')),
    )
    read_result = memory_mod.memory_editor(
        'memory',
        op='patch',
        old_text='old',
        new_text='new',
    )

    monkeypatch.setattr(
        memory_mod,
        'MemoryRemoteStore',
        lambda: FakeMemoryStore({'memory': 'old'}, write_error=RuntimeError('draft conflict')),
    )
    monkeypatch.setattr(memory_mod, '_validate_generated_content', lambda memory_type, content: content)
    write_result = memory_mod.memory_editor(
        'memory',
        op='patch',
        old_text='old',
        new_text='new',
    )

    assert read_result['success'] is False
    assert 'Failed to read memory via RemoteFS: backend down' in read_result['error']['reason']
    assert write_result['success'] is False
    assert 'Failed to write memory via RemoteFS: draft conflict' in write_result['error']['reason']


def test_read_memory_reads_remote_fs(monkeypatch):
    store = FakeMemoryStore({'memory': 'remote memory'})
    monkeypatch.setattr(memory_reader_mod, 'MemoryRemoteStore', lambda: store)

    result = memory_reader_mod.read_memory('memory')

    assert result['success'] is True
    assert result['tool'] == 'read_memory'
    assert result['result'] == {
        'target': 'memory',
        'content': 'remote memory',
        'content_length': len('remote memory'),
    }


def test_skill_editor_create_file_tools_remove_core_paths(monkeypatch):
    create_calls = []
    remove_calls = []
    replace_calls = []
    remote_fs = object()

    monkeypatch.setattr(
        skill_editor_mod.lazyllm,
        'globals',
        {'agentic_config': {'user_id': 'user-1', 'session_id': 'session-1'}},
    )
    monkeypatch.setattr(
        skill_editor_mod,
        'create_remote_skill',
        lambda *args, **kwargs: create_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        skill_editor_mod,
        'remove_remote_skill',
        lambda *args, **kwargs: remove_calls.append((args, kwargs)),
    )

    existing_content = (
        '---\n'
        'name: existing\n'
        'category: writing\n'
        'description: Existing skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    monkeypatch.setattr(
        skill_operations_mod,
        '_list_skill_files',
        lambda category, name, **kwargs: {
            'SKILL.md': existing_content,
            'references/old.md': 'old reference\n',
        },
    )

    def fake_replace_skill_package_files(category, name, before, after, **kwargs):
        assert kwargs == {'fs': remote_fs}
        replace_calls.append((category, name, before, after))
        return {
            'written': sorted(path for path in after if before.get(path) != after.get(path)),
            'deleted': sorted(set(before) - set(after)),
        }

    monkeypatch.setattr(skill_operations_mod, '_replace_skill_package_files', fake_replace_skill_package_files)

    content = (
        '---\n'
        'name: new_skill\n'
        'category: drafts\n'
        'description: A test skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    tool_group = skill_editor_mod.SkillEditorToolGroup(remote_fs=remote_fs)
    create_result = tool_group.create_skill(
        'new_skill',
        category='drafts',
        content=content,
    )
    patch_result = tool_group.patch_file(
        'existing',
        category='writing',
        path='SKILL.md',
        old_text='Use this skill for tests.',
        new_text='Use this skill for focused tests.',
        reason='patch skill body',
    )
    file_create_result = tool_group.create_file(
        'writing/existing',
        category='writing',
        path='scripts/check.py',
        content='print("ok")\n',
        reason='new helper script',
    )
    delete_result = tool_group.delete_file(
        'existing',
        category='writing',
        path='references/old.md',
        reason='remove stale reference',
    )
    remove_result = tool_group.remove_skill('existing', category='writing')

    assert create_result['success'] is True
    assert create_result['tool'] == 'create_skill'
    assert patch_result['success'] is True
    assert patch_result['tool'] == 'patch_file'
    assert file_create_result['success'] is True
    assert file_create_result['tool'] == 'create_file'
    assert delete_result['success'] is True
    assert delete_result['tool'] == 'delete_file'
    assert remove_result['success'] is True
    assert remove_result['tool'] == 'remove_skill'
    assert create_result['result'] == {
        'status': 'created',
        'message': 'Skill was created and is now active.',
    }
    assert patch_result['result']['status'] == 'patched'
    assert patch_result['result']['touched_files'] == ['SKILL.md']
    assert patch_result['result']['written_files'] == ['SKILL.md']
    assert patch_result['result']['deleted_files'] == []
    assert patch_result['result']['summary'] == 'patch skill body'
    assert file_create_result['result']['status'] == 'created'
    assert file_create_result['result']['written_files'] == ['scripts/check.py']
    assert file_create_result['result']['deleted_files'] == []
    assert delete_result['result']['status'] == 'deleted'
    assert delete_result['result']['written_files'] == []
    assert delete_result['result']['deleted_files'] == ['references/old.md']
    assert remove_result['result'] == {
        'status': 'removed',
        'message': 'Skill was removed and is no longer active.',
    }
    assert create_calls == [(('drafts', 'new_skill', content), {'fs': remote_fs})]
    assert remove_calls == [(('writing', 'existing'), {'fs': remote_fs})]
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
                'references/old.md': 'old reference\n',
            },
        ),
        (
            'writing',
            'existing',
            {'SKILL.md': existing_content, 'references/old.md': 'old reference\n'},
            {
                'SKILL.md': existing_content,
                'references/old.md': 'old reference\n',
                'scripts/check.py': 'print("ok")\n',
            },
        ),
        (
            'writing',
            'existing',
            {'SKILL.md': existing_content, 'references/old.md': 'old reference\n'},
            {'SKILL.md': existing_content},
        ),
    ]


def test_skill_editor_identity_accepts_matching_category_key():
    resolved = skill_editor_mod.resolve_skill_editor_identity(
        'research/web-research',
        'research',
        'create_file',
    )
    omitted_category = skill_editor_mod.resolve_skill_editor_identity(
        'research/web-research',
        None,
        'create_file',
    )
    conflict = skill_editor_mod.resolve_skill_editor_identity(
        'research/web-research',
        'writing',
        'create_file',
    )

    assert resolved == {'category': 'research', 'name': 'web-research'}
    assert omitted_category == {'category': 'research', 'name': 'web-research'}
    assert conflict['error'] == (
        "Skill key 'research/web-research' conflicts with category 'writing'; "
        'they must refer to the same category.'
    )


def test_skill_editor_resolves_unique_bare_name_like_skill_manager(monkeypatch):
    replace_calls = []
    existing_content = (
        '---\n'
        'name: web-research\n'
        'category: research\n'
        'description: Research skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    fs = FakeSkillRemoteFS({('research', 'web-research'): existing_content})
    monkeypatch.setattr(skill_editor_mod.lazyllm, 'globals', {'agentic_config': {}})
    monkeypatch.setattr(
        skill_operations_mod,
        '_list_skill_files',
        lambda category, name, **kwargs: {'SKILL.md': existing_content},
    )

    def fake_replace_skill_package_files(category, name, before, after, **kwargs):
        replace_calls.append((category, name, before, after))
        return {'written': ['SKILL.md'], 'deleted': []}

    monkeypatch.setattr(skill_operations_mod, '_replace_skill_package_files', fake_replace_skill_package_files)

    result = skill_editor_mod.SkillEditorToolGroup(remote_fs=fs).patch_file(
        'web-research',
        path='SKILL.md',
        old_text='Use this skill for tests.',
        new_text='Use this skill for focused tests.',
    )

    assert result['success'] is True
    assert replace_calls[0][0:2] == ('research', 'web-research')


def test_skill_editor_reports_ambiguous_bare_name_like_skill_manager():
    content = (
        '---\n'
        'name: web-research\n'
        'description: Research skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    fs = FakeSkillRemoteFS({
        ('custom', 'web-research'): content,
        ('research', 'web-research'): content,
    })

    result = skill_editor_mod.SkillEditorToolGroup(remote_fs=fs).patch_file(
        'web-research',
        path='SKILL.md',
        old_text='old',
        new_text='new',
    )

    assert result['success'] is False
    assert result['error']['reason'] == (
        "Ambiguous skill name 'web-research'; use the full skill key such as 'custom/web-research'."
    )


def test_skill_remote_store_create_mkdirs_and_remove_trashes():
    calls = []

    class FakeRemoteFS:
        def mkdir(self, path, create_parents=True):
            calls.append(('mkdir', path, create_parents))

        def write(self, path, content):
            calls.append(('write', path, content))

        def trash(self, path):
            calls.append(('trash', path))

    fs = FakeRemoteFS()

    create_result = skill_remote_store_mod.create_remote_skill('drafts', 'new-skill', 'content', fs=fs)
    remove_result = skill_remote_store_mod.remove_remote_skill('drafts', 'new-skill', fs=fs)

    assert create_result['action'] == 'create'
    assert remove_result['action'] == 'remove'
    assert calls == [
        ('mkdir', 'remote://skills/drafts/new-skill', True),
        ('write', 'remote://skills/drafts/new-skill/SKILL.md', 'content'),
        ('trash', 'remote://skills/drafts/new-skill'),
    ]


def test_skill_editor_rejects_missing_skill_without_write(monkeypatch):
    calls = []

    monkeypatch.setattr(skill_editor_mod.lazyllm, 'globals', {'agentic_config': {}})
    monkeypatch.setattr(skill_operations_mod, '_replace_skill_package_files', lambda *args, **kwargs: calls.append(args))

    def raise_missing_skill(*args, **kwargs):
        raise FileNotFoundError('skill not found')

    monkeypatch.setattr(skill_operations_mod, '_list_skill_files', raise_missing_skill)

    result = skill_editor_mod.SkillEditorToolGroup().patch_file(
        'missing',
        category='writing',
        path='SKILL.md',
        old_text='old',
        new_text='new',
    )

    assert result == {
        'success': False,
        'tool': 'patch_file',
        'error': {
            'reason': 'Failed to load or edit skill package: skill not found',
        },
    }
    assert calls == []


def test_skill_editor_create_and_delete_file_reject_skill_md(monkeypatch):
    replace_calls = []
    existing_content = (
        '---\n'
        'name: existing\n'
        'category: writing\n'
        'description: Existing skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    monkeypatch.setattr(skill_editor_mod.lazyllm, 'globals', {'agentic_config': {}})
    monkeypatch.setattr(
        skill_operations_mod,
        '_list_skill_files',
        lambda category, name, **kwargs: {'SKILL.md': existing_content, 'references/doc.md': 'doc\n'},
    )
    monkeypatch.setattr(skill_operations_mod, '_replace_skill_package_files', lambda *args, **kwargs: replace_calls.append(args))

    tool_group = skill_editor_mod.SkillEditorToolGroup()

    create_result = tool_group.create_file('existing', category='writing', path='SKILL.md', content='content')
    create_existing_result = tool_group.create_file(
        'existing',
        category='writing',
        path='references/doc.md',
        content='new doc\n',
    )
    delete_result = tool_group.delete_file('existing', category='writing', path='SKILL.md')

    assert create_result['success'] is False
    assert create_result['error']['reason'] == (
        'create_file cannot create or overwrite SKILL.md; use edit_file or patch_file instead.'
    )
    assert create_existing_result['success'] is False
    assert create_existing_result['error']['reason'] == 'File already exists; use edit_file or patch_file to modify it.'
    assert delete_result['success'] is False
    assert delete_result['error']['reason'] == (
        'SKILL.md cannot be deleted with delete_file; use remove_skill to remove the whole skill package.'
    )
    assert replace_calls == []


def test_skill_editor_validates_skill_md_edits(monkeypatch):
    replace_calls = []
    existing_content = (
        '---\n'
        'name: existing\n'
        'category: writing\n'
        'description: Existing skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    renamed_content = existing_content.replace('name: existing', 'name: renamed')
    monkeypatch.setattr(skill_editor_mod.lazyllm, 'globals', {'agentic_config': {}})
    monkeypatch.setattr(
        skill_operations_mod,
        '_list_skill_files',
        lambda category, name, **kwargs: {'SKILL.md': existing_content},
    )
    monkeypatch.setattr(skill_operations_mod, '_replace_skill_package_files', lambda *args, **kwargs: replace_calls.append(args))

    tool_group = skill_editor_mod.SkillEditorToolGroup()

    invalid_result = tool_group.edit_file('existing', category='writing', path='SKILL.md', content='not a skill')
    rename_result = tool_group.edit_file('existing', category='writing', path='SKILL.md', content=renamed_content)
    patch_rename_result = tool_group.patch_file(
        'existing',
        category='writing',
        path='SKILL.md',
        old_text='name: existing',
        new_text='name: renamed',
    )

    assert invalid_result['success'] is False
    assert invalid_result['error']['reason'] == 'SKILL.md must contain YAML frontmatter.'
    assert rename_result['success'] is False
    assert rename_result['error']['reason'] == 'SKILL.md frontmatter name/category cannot be changed; use rename_skill.'
    assert patch_rename_result['success'] is False
    assert patch_rename_result['error']['reason'] == (
        'SKILL.md frontmatter name/category cannot be changed; use rename_skill.'
    )
    assert replace_calls == []


def test_skill_editor_renames_package(monkeypatch):
    rename_calls = []
    remote_fs = object()
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
        'list_skill_files',
        lambda category, name, **kwargs: {'SKILL.md': existing_content, 'references/doc.md': 'doc\n'},
    )
    monkeypatch.setattr(
        skill_editor_mod,
        'rename_skill_package',
        lambda *args, **kwargs: rename_calls.append((args, kwargs)),
    )

    result = skill_editor_mod.SkillEditorToolGroup(remote_fs=remote_fs).rename_skill(
        'existing',
        category='writing',
        new_name='renamed',
        new_category='drafts',
    )

    assert result['success'] is True
    assert result['result']['status'] == 'renamed'
    assert result['result']['old'] == {'category': 'writing', 'name': 'existing'}
    assert result['result']['new'] == {'category': 'drafts', 'name': 'renamed'}
    assert rename_calls[0][0][:4] == ('writing', 'existing', 'drafts', 'renamed')
    assert rename_calls[0][1]['fs'] is remote_fs
    assert 'name: renamed' in rename_calls[0][1]['skill_content']
    assert 'category: drafts' in rename_calls[0][1]['skill_content']
