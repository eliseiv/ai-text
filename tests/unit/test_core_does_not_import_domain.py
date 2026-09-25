"""THE invariant of the template: the core never imports ``app.domain``.

An AST scan, not a grep — and it descends into FUNCTION BODIES too. A deferred import inside a
method (``def foo(): from app.domain import X``) is exactly how the source's ``config.py`` ended up
importing ``app.chat.presets``, which made the core unable to start without its domain. A
module-level-only check would have called that file clean.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "app"
_DOMAIN_PACKAGE = "app.domain"

# THE single coupling point: the loader — and nothing else — may name the domain, inside
# a `try: … except ImportError:` so the empty template still runs. Every other core module that
# names the domain is the defect this file exists to catch.
_LOADER = _SRC / "extensions" / "loader.py"


def _core_modules() -> list[pathlib.Path]:
    return sorted(
        path
        for path in _SRC.rglob("*.py")
        if "domain" not in path.relative_to(_SRC).parts
        and "__pycache__" not in path.parts
        and path != _LOADER
    )


def _domain_imports(tree: ast.AST) -> list[str]:
    """Every import of ``app.domain`` ANYWHERE in the module — including inside functions."""
    hits: list[str] = []
    for node in ast.walk(tree):  # ast.walk descends into function bodies
        if isinstance(node, ast.Import):
            hits += [
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if alias.name == _DOMAIN_PACKAGE or alias.name.startswith(f"{_DOMAIN_PACKAGE}.")
            ]
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == _DOMAIN_PACKAGE or node.module.startswith(f"{_DOMAIN_PACKAGE}."))
        ):
            hits.append(f"line {node.lineno}: from {node.module} import ...")
    return hits


def test_the_scan_actually_finds_domain_imports() -> None:
    """Guard for the guard: a scanner that finds nothing anywhere would pass vacuously."""
    tree = ast.parse(
        "def f():\n    from app.domain.provider import X\n    return X\n"
        "import app.domain.models\n"
    )
    assert len(_domain_imports(tree)) == 2


def test_core_modules_exist_and_are_scanned() -> None:
    modules = _core_modules()
    assert len(modules) > 20
    assert not any("domain" in m.parts for m in modules)


@pytest.mark.parametrize("module", _core_modules(), ids=lambda p: str(p.name))
def test_core_module_does_not_import_the_domain(module: pathlib.Path) -> None:
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    hits = _domain_imports(tree)
    assert not hits, f"{module} imports the domain: {hits}"


def test_the_only_coupling_point_is_the_loader_and_it_degrades_to_the_empty_registry() -> None:
    """The one allowed exception, pinned: the import is guarded by ``except ImportError``, so a
    template with no domain still starts (with ``EMPTY_REGISTRY``)."""
    tree = ast.parse(_LOADER.read_text(encoding="utf-8"), filename=str(_LOADER))
    assert _domain_imports(tree), "the loader is the coupling point — it must import the domain"

    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        and _domain_imports(ast.Module(body=node.body, type_ignores=[]))
        and any(isinstance(h.type, ast.Name) and h.type.id == "ImportError" for h in node.handlers)
    ]
    assert guarded, "the domain import must be inside try/except ImportError (empty template runs)"

    from app.extensions.loader import load_registry
    from app.extensions.registry import DomainRegistry

    assert isinstance(load_registry(), DomainRegistry)
