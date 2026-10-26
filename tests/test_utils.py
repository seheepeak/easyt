"""What may go in easyt.utils: the rule in its __init__.py."""

import ast
from importlib.util import resolve_name
from pathlib import Path

import easyt.utils

UTILS = Path(easyt.utils.__file__).parent


def _imported(path: Path) -> list[str]:
    """Every module and name `path` imports, in full: `from X import Y` as X.Y."""
    package = ".".join(path.relative_to(UTILS.parent.parent).parent.parts)
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = resolve_name("." * node.level + (node.module or ""), package)
            names += [f"{base}.{alias.name}" for alias in node.names]
    return names


def test_utils_import_nothing_from_the_rest_of_easyt():
    for path in UTILS.rglob("*.py"):
        for name in _imported(path):
            parts = name.split(".")
            assert parts[0] != "easyt" or parts[:2] == ["easyt", "utils"], (
                f"{path.name} imports {name}"
            )
