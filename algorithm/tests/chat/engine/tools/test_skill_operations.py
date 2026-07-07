from __future__ import annotations

import pytest

from lazymind.chat.engine.tools.infra import skill_operations as skill_operations_mod
from lazymind.chat.engine.tools.infra.skill_operations import (
    create_skill_file,
    delete_skill_file,
    edit_skill_file,
    fuzzy_find_and_replace,
    normalize_skill_package_path,
)


@pytest.mark.parametrize('path', ['/x', '../x', 'refs/../x', 'refs//x', r'refs\x', ''])
def test_normalize_skill_package_path_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        normalize_skill_package_path(path)


def test_skill_file_operations_create_and_delete_supporting_files(monkeypatch):
    existing_content = (
        '---\n'
        'name: existing\n'
        'category: writing\n'
        'description: Existing skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    fs = object()
    replace_calls = []

    monkeypatch.setattr(
        skill_operations_mod,
        '_list_skill_files',
        lambda category, name, **kwargs: {'SKILL.md': existing_content, 'references/old.md': 'old\n'},
    )

    def fake_replace(category, name, before, after, **kwargs):
        assert kwargs == {'fs': fs}
        replace_calls.append((category, name, before, after))
        return {
            'written': sorted(path for path in after if before.get(path) != after.get(path)),
            'deleted': sorted(set(before) - set(after)),
        }

    monkeypatch.setattr(skill_operations_mod, '_replace_skill_package_files', fake_replace)

    create_result = create_skill_file('writing', 'existing', 'scripts/check.py', 'print("ok")\n', fs=fs)
    delete_result = delete_skill_file('writing', 'existing', 'references/old.md', fs=fs)

    assert create_result['status'] == 'created'
    assert create_result['written_files'] == ['scripts/check.py']
    assert delete_result['status'] == 'deleted'
    assert delete_result['deleted_files'] == ['references/old.md']
    assert len(replace_calls) == 2


def test_edit_skill_file_rejects_skill_identity_change(monkeypatch):
    existing_content = (
        '---\n'
        'name: existing\n'
        'category: writing\n'
        'description: Existing skill.\n'
        '---\n'
        'Use this skill for tests.\n'
    )
    renamed_content = existing_content.replace('name: existing', 'name: renamed')
    monkeypatch.setattr(
        skill_operations_mod,
        '_list_skill_files',
        lambda category, name, **kwargs: {'SKILL.md': existing_content},
    )

    with pytest.raises(ValueError, match='SKILL.md frontmatter name/category cannot be changed'):
        edit_skill_file('writing', 'existing', 'SKILL.md', renamed_content, fs=object())


def test_fuzzy_find_and_replace_exact_match():
    edited, count, strategy, error = fuzzy_find_and_replace('alpha beta\n', 'beta', 'gamma')

    assert error is None
    assert count == 1
    assert strategy == 'exact'
    assert edited == 'alpha gamma\n'


def test_fuzzy_find_and_replace_line_trimmed_match():
    edited, count, strategy, error = fuzzy_find_and_replace(
        'first\n  old value  \nlast\n',
        'old value\nlast',
        'new value\nlast',
    )

    assert error is None
    assert count == 1
    assert strategy == 'line_trimmed'
    assert edited == 'first\n  new value\n  last\n'


def test_fuzzy_find_and_replace_indentation_flexible_match_reindents():
    edited, count, strategy, error = fuzzy_find_and_replace(
        'def f():\n    if ready:\n        return "old"\n',
        '  if ready:\n    return "old"',
        '  if ready:\n    return "new"',
    )

    assert error is None
    assert count == 1
    assert strategy in {'line_trimmed', 'indentation_flexible'}
    assert edited == 'def f():\n    if ready:\n      return "new"\n'


def test_fuzzy_find_and_replace_unicode_normalized_match():
    edited, count, strategy, error = fuzzy_find_and_replace(
        'Use smart \u201cquotes\u201d here.\n',
        'smart "quotes"',
        'plain quotes',
    )

    assert error is None
    assert count == 1
    assert strategy == 'unicode_normalized'
    assert edited == 'Use plain quotes here.\n'


def test_fuzzy_find_and_replace_requires_unique_match_unless_replace_all():
    unchanged, count, strategy, error = fuzzy_find_and_replace('old old', 'old', 'new')
    edited, replace_all_count, replace_all_strategy, replace_all_error = fuzzy_find_and_replace(
        'old old',
        'old',
        'new',
        replace_all=True,
    )

    assert unchanged == 'old old'
    assert count == 0
    assert strategy is None
    assert error == 'Found 2 matches for old_text. Provide more context to make it unique, or use replace_all=True.'
    assert edited == 'new new'
    assert replace_all_count == 2
    assert replace_all_strategy == 'exact'
    assert replace_all_error is None
