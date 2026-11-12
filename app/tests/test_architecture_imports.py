"""Layer-direction and cycle guard for the Python package.

The project is intentionally layered:

    adapters -> application -> domain
    adapters/application -> transport -> domain

``domain`` is pure computation and must not reach up into application or
adapters.  ``transport`` implements the wire/native boundary and must not reach
up either.  A few lazy, documented exceptions exist; they are listed below so
a new one cannot be added silently.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

REPLICA = Path(__file__).resolve().parents[1] / 'replica'
LAYERS = {'adapters', 'application', 'compat', 'diagnostics', 'domain', 'transport'}

# (source module, target module) exceptions that are lazy and documented.
ALLOWED_DOMAIN_TO_TRANSPORT = {
    ('domain.crypto', 'transport.rustcore'),
    ('domain.rundata', 'transport.rustcore'),
    ('domain.rundata', 'transport.obs'),
}
ALLOWED_CYCLES = [{'domain.rundata', 'transport.obs'}]

UPWARD_FORBIDDEN = {
    'domain': {'application', 'adapters', 'compat', 'diagnostics'},
    'transport': {'application', 'adapters', 'compat', 'diagnostics'},
    'application': {'adapters', 'compat'},
    'compat': {'application', 'adapters', 'diagnostics'},
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(REPLICA).with_suffix('')
    parts = list(rel.parts)
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts) if parts else '(root)'


def _package_parts(path: Path) -> list[str]:
    rel = path.relative_to(REPLICA).parent
    return [] if str(rel) == '.' else list(rel.parts)


def _normalise(module: str) -> str:
    return module[8:] if module.startswith('replica.') else module


def _resolve(path: Path, node: ast.ImportFrom | ast.Import) -> list[str]:
    """Return normalised replica module targets for one import node."""
    if isinstance(node, ast.Import):
        return [
            _normalise(alias.name)
            for alias in node.names
            if alias.name == 'replica' or alias.name.startswith('replica.')
        ]

    if node.level:
        package = _package_parts(path)
        keep = len(package) - (node.level - 1)
        base = package[:keep] if keep > 0 else []
        if node.module:
            base.extend(node.module.split('.'))
        candidates = ['.'.join(base)] if node.module else []
        if not node.module:
            candidates = ['.'.join(base + [alias.name]) for alias in node.names]
        elif (REPLICA.joinpath(*base)).is_dir():
            candidates = ['.'.join(base + [alias.name]) for alias in node.names]
    elif node.module and (node.module == 'replica' or node.module.startswith('replica.')):
        base = _normalise(node.module).split('.')
        if (REPLICA.joinpath(*base)).is_dir():
            candidates = ['.'.join(base + [alias.name]) for alias in node.names]
        else:
            candidates = ['.'.join(base)]
    else:
        return []

    return [target for target in (_normalise(c) for c in candidates) if target]


def _layer(module: str) -> str:
    return module.split('.')[0] if module.split('.')[0] in LAYERS else '(root)'


def _build_graph():
    modules = {
        _module_name(p)
        for p in REPLICA.rglob('*.py')
        if '__pycache__' not in p.parts
    }
    edges: defaultdict[str, set[str]] = defaultdict(set)
    pairs: set[tuple[str, str]] = set()
    for path in sorted(REPLICA.rglob('*.py')):
        if '__pycache__' in path.parts:
            continue
        source = _module_name(path)
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        except SyntaxError as exc:
            raise AssertionError(f'{source}: cannot parse: {exc}') from exc
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _resolve(path, node):
                if target not in modules:
                    continue
                edges[source].add(target)
                pairs.add((source, target))
    return modules, edges, pairs


def test_no_upward_layer_imports():
    _, _, pairs = _build_graph()
    violations = []
    for source, target in sorted(pairs):
        source_layer, target_layer = _layer(source), _layer(target)
        forbidden = UPWARD_FORBIDDEN.get(source_layer, set())
        if target_layer in forbidden:
            violations.append(f'{source} -> {target}')
        if source_layer == 'domain' and target_layer == 'transport':
            if (source, target) not in ALLOWED_DOMAIN_TO_TRANSPORT:
                violations.append(f'{source} -> {target} (domain->transport not allowlisted)')
    assert not violations, 'upward or unallowlisted imports:\n' + '\n'.join(violations)


def test_no_new_import_cycles():
    modules, edges, _ = _build_graph()
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    components: list[set[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in edges.get(node, ()):
            if nxt not in indices:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], indices[nxt])
        if low[node] == indices[node]:
            component = set()
            while True:
                item = stack.pop()
                on_stack.remove(item)
                component.add(item)
                if item == node:
                    break
            if len(component) > 1:
                components.append(component)

    for module in sorted(modules):
        if module not in indices:
            visit(module)

    unexpected = [sorted(c) for c in components if c not in ALLOWED_CYCLES]
    assert not unexpected, 'new import cycles: ' + repr(unexpected)
