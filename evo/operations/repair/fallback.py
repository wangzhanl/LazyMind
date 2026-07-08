from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .code_index import CodeIndex, CodeSymbol, DOMAIN_ROOTS, symbol_at

TEXT_ARGS = {'answer', 'content', 'message', 'prompt', 'query', 'question', 'text'}
LIST_ARGS = {'chunk_ids', 'chunks', 'doc_ids', 'docs', 'files', 'items', 'messages', 'results'}
DICT_ARGS = {'config', 'filters', 'metadata', 'options', 'params', 'payload'}


def generate_fallback_patch(root: Path, index: CodeIndex, localization: Mapping[str, Any]) -> dict[str, Any]:
    for symbol in _candidate_symbols(index, localization):
        result = _patch_symbol(root, symbol)
        if result.get('status') == 'patched':
            return result
    return _last_resort_patch(root)


def _candidate_symbols(index: CodeIndex, localization: Mapping[str, Any]) -> tuple[CodeSymbol, ...]:
    ranked = []
    for row in localization.get('ranked_symbols') or ():
        if not isinstance(row, Mapping):
            continue
        symbol = symbol_at(index, str(row.get('path') or ''), str(row.get('symbol') or ''))
        if symbol:
            ranked.append(symbol)
    seen = {(symbol.path, symbol.qualname) for symbol in ranked}
    domain = str(localization.get('domain') or '').strip()
    rest = [
        symbol for symbol in index.symbols
        if symbol.kind == 'function'
        and (symbol.path, symbol.qualname) not in seen
        and (not domain or domain == 'mixed' or symbol.domain == domain)
    ]
    if not rest:
        rest = [
            symbol for symbol in index.symbols
            if symbol.kind == 'function' and (symbol.path, symbol.qualname) not in seen
        ]
    return tuple([*ranked, *rest])


def _patch_symbol(root: Path, symbol: CodeSymbol) -> dict[str, Any]:
    if not any(symbol.path == base or symbol.path.startswith(f'{base}/') for base in DOMAIN_ROOTS):
        return {'status': 'skipped', 'reason': 'outside_domain_roots', 'symbol': symbol.qualname}
    path = root / symbol.path
    try:
        source = path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=symbol.path)
    except (OSError, SyntaxError) as exc:
        return {'status': 'skipped', 'reason': type(exc).__name__, 'symbol': symbol.qualname}
    node = _find_function(tree, symbol.qualname)
    if node is None:
        return {'status': 'skipped', 'reason': 'symbol_not_found', 'symbol': symbol.qualname}
    guard = _guard_lines(node)
    if not guard:
        return {'status': 'skipped', 'reason': 'no_suitable_argument', 'symbol': symbol.qualname}
    if guard[0].strip() in source:
        return {'status': 'skipped', 'reason': 'guard_already_present', 'symbol': symbol.qualname}
    lines = source.splitlines()
    insert = _insertion(node, lines)
    if insert['mode'] == 'expand_inline':
        lines[int(insert['index'])] = str(insert['header'])
        lines[int(insert['index']) + 1:int(insert['index']) + 1] = [
            *[f"{insert['indent']}{line}" for line in guard],
            f"{insert['indent']}{insert['tail']}",
        ]
    else:
        lines[int(insert['index']):int(insert['index'])] = [f"{insert['indent']}{line}" for line in guard]
    path.write_text('\n'.join(lines) + ('\n' if source.endswith('\n') else ''), encoding='utf-8')
    return {
        'status': 'patched',
        'path': symbol.path,
        'symbol': symbol.qualname,
        'change_intent': 'fallback defensive input normalization',
    }


def _find_function(tree: ast.AST, qualname: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    target = qualname.split('.')
    found: ast.FunctionDef | ast.AsyncFunctionDef | None = None

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit(node)

        def _visit(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            nonlocal found
            current = [*self.stack, node.name]
            if current == target:
                found = node
                return
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

    Visitor().visit(tree)
    return found


def _guard_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = [arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if arg.arg not in {'self', 'cls'}]
    for name in args:
        if name in TEXT_ARGS or any(token in name for token in TEXT_ARGS):
            return [f'if isinstance({name}, str):', f'    {name} = {name}.strip()']
    for name in args:
        if name in LIST_ARGS or name.endswith(('s', '_list')):
            return [f'if {name} is None:', f'    {name} = []']
    for name in args:
        if name in DICT_ARGS or name.endswith(('_dict', '_map')):
            return [f'if {name} is None:', f'    {name} = {{}}']
    return []


def _insertion(node: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str]) -> dict[str, Any]:
    body = list(node.body)
    if body and body[0].lineno == node.lineno:
        line = lines[node.lineno - 1]
        body_col = int(getattr(body[0], 'col_offset', len(line)))
        header = line[:body_col].rstrip()
        tail = line[body_col:].strip()
        if header.endswith(':') and tail:
            return {'mode': 'expand_inline', 'index': node.lineno - 1, 'header': header,
                    'tail': tail, 'indent': ' ' * (node.col_offset + 4)}
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], 'value', None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        index = int(getattr(body[0], 'end_lineno', body[0].lineno))
    else:
        index = int(body[0].lineno - 1 if body else node.lineno)
    return {'mode': 'insert', 'index': index, 'indent': _indent(lines[index] if index < len(lines) else '')}


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def _last_resort_patch(root: Path) -> dict[str, Any]:
    target = next((root / base / '__init__.py' for base in DOMAIN_ROOTS if (root / base).exists()), None)
    if target is None:
        return {'status': 'failed', 'reason': 'no_domain_root'}
    target.parent.mkdir(parents=True, exist_ok=True)
    source = target.read_text(encoding='utf-8') if target.exists() else ''
    helper = (
        '\n\ndef normalize_repair_text(value):\n'
        '    if isinstance(value, str):\n'
        '        return value.strip()\n'
        '    return value\n'
    )
    if 'def normalize_repair_text(' in source:
        return {'status': 'failed', 'reason': 'last_resort_already_present'}
    target.write_text(source.rstrip() + helper + '\n', encoding='utf-8')
    return {
        'status': 'patched',
        'path': target.relative_to(root).as_posix(),
        'symbol': 'normalize_repair_text',
        'change_intent': 'fallback domain text normalization helper',
        'last_resort': True,
    }
