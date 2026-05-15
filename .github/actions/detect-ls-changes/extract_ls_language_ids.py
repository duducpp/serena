#!/usr/bin/env python3
"""
Extract language IDs from changed language server implementations and/or solidlsp tests.

Language server modules: parse ``super().__init__(..., language_id=...)`` / 4th positional arg.

Tests under ``test/solidlsp``: parse ``pytest.mark.<language>``, ``pytest.mark.parametrize``
with ``Language.*`` / shared list constants (e.g. ``PYTHON_BACKEND_LANGUAGES``), and
``pytestmark = ...``. Those marks are matched to ``Language`` values in ``ls_config.py`` to
resolve which ``test/solidlsp/<package>`` directory to run (never ``test/solidlsp`` alone —
root-level modules under ``test/solidlsp`` are skipped). No hard-coded directory aliases.

Usage:
  python scripts/extract_ls_language_ids.py                      # diff vs HEAD
  python scripts/extract_ls_language_ids.py --base main          # diff vs main
  python scripts/extract_ls_language_ids.py path/to/file.py      # explicit files
  python scripts/extract_ls_language_ids.py --format github ...  # write to GITHUB_OUTPUT
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
from enum import Enum
from pathlib import Path


def _repo_root() -> Path:
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", "")).resolve()
    # Repo root: .../.github/actions/detect-ls-changes/this_file.py -> four parents up
    fallback = Path(__file__).resolve().parents[3]

    for root in (workspace, fallback):
        if (root / "src" / "solidlsp" / "ls_config.py").is_file():
            return root

    raise AssertionError(f"Could not find repo root in {workspace} or {fallback}")


_TEST_ROOT = _repo_root() / "test" / "solidlsp"

# ``@pytest.mark.parametrize("<name>", [...], indirect=True)`` where values are ``Language`` enums
_PARAM_NAMES_WITH_LANGUAGE = frozenset(
    {"language_server", "repo_path", "ls_with_ignored_dirs", "project", "project_with_ls"}
)

_CONFTEST_LANGUAGE_LISTS: dict[str, frozenset[str]] | None = None
_LANG_ID_TO_TEST_DIRS: dict[str, frozenset[str]] | None = None


def _load_language_enum() -> type[Enum]:
    """Load ``Language`` from ``ls_config.py`` without importing ``solidlsp`` package ``__init__``."""

    ls_config_path = _repo_root() / "src" / "solidlsp" / "ls_config.py"

    if not ls_config_path.is_file():
        raise FileNotFoundError(f"Expected ls_config at {ls_config_path}")

    mod_name = "_serena_extract_ls_config_stub"
    spec = importlib.util.spec_from_file_location(mod_name, ls_config_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load spec for {ls_config_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lang = getattr(module, "Language", None)
    if not isinstance(lang, type) or not issubclass(lang, Enum):
        raise TypeError("ls_config.Language is missing or not an Enum")
    return lang

Language = _load_language_enum()

_LANGUAGE_ID_VALUES = frozenset(lang.value for lang in Language)

def _language_id_from_ast(expr: ast.expr) -> str | None:
    """Resolve a ``language_id`` AST node to a string using ``Language`` enum values."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        if expr.value in _LANGUAGE_ID_VALUES:
            return expr.value
        return None
    if (
        isinstance(expr, ast.Attribute)
        and isinstance(expr.value, ast.Name)
        and expr.value.id == "Language"
    ):
        member = getattr(Language, expr.attr, None)
        if isinstance(member, Language):
            return member.value
    return None


def _language_id_from_super_init_call(stmt: ast.Call) -> str | None:
    """``SolidLanguageServer.__init__`` takes ``language_id`` as 4th positional or as keyword."""
    kw_lang: str | None = None
    for kw in stmt.keywords:
        if kw.arg == "language_id":
            kw_lang = _language_id_from_ast(kw.value)
            break
    if kw_lang is not None:
        return kw_lang
    if len(stmt.args) >= 4:
        return _language_id_from_ast(stmt.args[3])
    return None


def _conftest_language_list_map() -> dict[str, frozenset[str]]:
    """Top-level ``Name = [Language.*, ...]`` assignments from root test conftests."""
    global _CONFTEST_LANGUAGE_LISTS
    if _CONFTEST_LANGUAGE_LISTS is not None:
        return _CONFTEST_LANGUAGE_LISTS
    merged: dict[str, set[str]] = {}
    root = _repo_root()
    for rel in ("test/conftest.py", "test/solidlsp/conftest.py"):
        path = root.joinpath(*rel.split("/"))
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for name, langs in _module_level_language_list_assigns(tree.body).items():
            merged.setdefault(name, set()).update(langs)
    _CONFTEST_LANGUAGE_LISTS = {k: frozenset(v) for k, v in merged.items()}
    return _CONFTEST_LANGUAGE_LISTS


def _module_level_language_list_assigns(body: list[ast.stmt]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for node in body:
        if not isinstance(node, ast.Assign):
            continue
        langs = _language_values_in_list_or_tuple(node.value)
        if not langs:
            continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                out.setdefault(t.id, set()).update(langs)
    return out


def _language_values_in_list_or_tuple(expr: ast.expr) -> set[str]:
    if not isinstance(expr, (ast.List, ast.Tuple)):
        return set()
    return {lid for e in expr.elts if (lid := _language_id_from_ast(e)) is not None}


def _pytest_mark_segments(expr: ast.expr) -> tuple[str, ...] | None:
    """``pytest.mark.foo`` / ``pytest.mark.foo(...)`` -> ``("pytest", "mark", "foo")``."""
    if isinstance(expr, ast.Call):
        expr = expr.func
    parts: list[str] = []
    cur: ast.expr | None = expr
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name) or cur.id not in ("pytest", "_pytest"):
        return None
    parts.reverse()
    return (cur.id, *parts)


def _language_ids_from_parametrize_call(call: ast.Call, list_map: dict[str, frozenset[str]]) -> set[str]:
    segs = _pytest_mark_segments(call.func)
    if segs is None or len(segs) < 3 or segs[1] != "mark" or segs[2] != "parametrize":
        return set()
    if len(call.args) < 2:
        return set()
    first, second = call.args[0], call.args[1]
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return set()
    if first.value not in _PARAM_NAMES_WITH_LANGUAGE:
        return set()
    if isinstance(second, ast.Name) and second.id in list_map:
        return set(list_map[second.id])
    return _language_values_in_list_or_tuple(second)


def _language_ids_from_pytest_mark_decorator(dec: ast.expr, list_map: dict[str, frozenset[str]]) -> set[str]:
    if isinstance(dec, ast.Call):
        segs = _pytest_mark_segments(dec.func)
        if segs is not None and len(segs) >= 3 and segs[1] == "mark" and segs[2] == "parametrize":
            return _language_ids_from_parametrize_call(dec, list_map)
        if segs is not None and len(segs) >= 3 and segs[1] == "mark":
            leaf = segs[2]
            if leaf in _LANGUAGE_ID_VALUES:
                return {leaf}
        return set()
    segs = _pytest_mark_segments(dec)
    if segs is not None and len(segs) >= 3 and segs[1] == "mark":
        leaf = segs[2]
        if leaf in _LANGUAGE_ID_VALUES:
            return {leaf}
    return set()


def _language_ids_from_pytestmark_value(value: ast.expr, list_map: dict[str, frozenset[str]]) -> set[str]:
    if isinstance(value, (ast.List, ast.Tuple)):
        acc: set[str] = set()
        for elt in value.elts:
            acc |= _language_ids_from_pytest_mark_decorator(elt, list_map)
        return acc
    return _language_ids_from_pytest_mark_decorator(value, list_map)


def _iter_class_and_function_nodes(tree: ast.Module) -> list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef]:
    out: list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(node)
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(item)
    return out


def extract_language_ids_from_test_file(filepath: Path) -> set[str]:
    """Language enum values referenced by pytest marks / parametrizes in one test module."""
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))
    except (OSError, SyntaxError) as exc:
        print(f"skip {filepath}: {exc}", file=sys.stderr)
        return set()

    conftest_lists = _conftest_language_list_map()
    local_lists = _module_level_language_list_assigns(tree.body)
    list_map: dict[str, frozenset[str]] = {**conftest_lists, **{k: frozenset(v) for k, v in local_lists.items()}}

    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "pytestmark":
                    found |= _language_ids_from_pytestmark_value(node.value, list_map)

    for node in _iter_class_and_function_nodes(tree):
        for dec in node.decorator_list:
            found |= _language_ids_from_pytest_mark_decorator(dec, list_map)

    return found


def _solidlsp_test_package_path(test_file: Path) -> str | None:
    """``test/solidlsp/<package>`` repo-relative path, or ``None`` if the file is not under a package (i.e. directly under ``test/solidlsp``)."""
    root = _repo_root()
    rel = test_file.resolve().relative_to(root)
    parts = rel.parts
    if len(parts) >= 4 and parts[0] == "test" and parts[1] == "solidlsp":
        return "/".join(parts[:3])
    return None


def _language_id_to_test_dirs_index() -> dict[str, frozenset[str]]:
    global _LANG_ID_TO_TEST_DIRS
    if _LANG_ID_TO_TEST_DIRS is not None:
        return _LANG_ID_TO_TEST_DIRS
    index: dict[str, set[str]] = {}
    if not _TEST_ROOT.is_dir():
        _LANG_ID_TO_TEST_DIRS = {}
        return _LANG_ID_TO_TEST_DIRS
    for path in sorted(_TEST_ROOT.rglob("*.py")):
        if not path.is_file():
            continue
        pkg = _solidlsp_test_package_path(path)
        if pkg is None:
            continue
        for lid in extract_language_ids_from_test_file(path):
            index.setdefault(lid, set()).add(pkg)
    _LANG_ID_TO_TEST_DIRS = {k: frozenset(v) for k, v in index.items()}
    return _LANG_ID_TO_TEST_DIRS


def _resolve_repo_path(p: Path) -> Path:
    root = _repo_root()
    return p.resolve() if p.is_absolute() else (root / p).resolve()


def _is_language_server_source(path: Path) -> bool:
    try:
        path.relative_to(_repo_root() / "src" / "solidlsp" / "language_servers")
        return True
    except ValueError:
        return False


def _is_solidlsp_test_source(path: Path) -> bool:
    try:
        path.relative_to(_TEST_ROOT)
        return path.suffix == ".py"
    except ValueError:
        return False


def _collect_language_ids_from_inputs(files: list[Path]) -> set[str]:
    langs: set[str] = set()
    for raw in files:
        path = _resolve_repo_path(raw)
        if not path.is_file():
            continue
        if _is_language_server_source(path):
            langs |= {lid for _cls, lid in extract_language_ids(path)}
        elif _is_solidlsp_test_source(path):
            langs |= extract_language_ids_from_test_file(path)
    return langs


def _get_changed_files(base: str) -> list[Path]:
    import subprocess

    ls_dir_parts = ("src", "solidlsp", "language_servers")
    result = subprocess.run(
        ["git", "diff", "--name-only", base],
        capture_output=True,
        text=True,
        check=True,
    )
    paths: list[Path] = []
    for line in result.stdout.splitlines():
        p = Path(line)
        if p.suffix != ".py":
            continue
        if p.parts[: len(ls_dir_parts)] == ls_dir_parts:
            paths.append(p)
        elif len(p.parts) >= 2 and p.parts[0] == "test" and p.parts[1] == "solidlsp":
            paths.append(p)
    return paths


def extract_language_ids(filepath: Path) -> list[tuple[str, str]]:
    """Return [(class_name, language_id)] for every class in filepath."""
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))
    except (OSError, SyntaxError) as exc:
        print(f"skip {filepath}: {exc}", file=sys.stderr)
        return []

    results: list[tuple[str, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        for item in node.body:
            if not (isinstance(item, ast.FunctionDef) and item.name == "__init__"):
                continue

            for stmt in ast.walk(item):
                if not isinstance(stmt, ast.Call):
                    continue

                # Match super().__init__(...)
                func = stmt.func
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr == "__init__"
                    and isinstance(func.value, ast.Call)
                    and isinstance(func.value.func, ast.Name)
                    and func.value.func.id == "super"
                ):
                    continue

                lang_id = _language_id_from_super_init_call(stmt)
                if lang_id is not None:
                    results.append((node.name, lang_id))
                    break  # one super().__init__ per __init__ is enough

    return results


def collect(files: list[Path]) -> tuple[list[str], list[str]]:
    """Return (unique_language_ids, repo-relative pytest dirs).

    Each dir is ``test/solidlsp/<package>``; ``test/solidlsp`` root alone is never emitted.
    """
    langs = _collect_language_ids_from_inputs(files)
    index = _language_id_to_test_dirs_index()
    test_paths: set[str] = set()
    for lid in langs:
        test_paths |= set(index.get(lid, ()))
    return sorted(langs), sorted(test_paths)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract language_id from changed language server sources and/or test/solidlsp tests"
    )
    parser.add_argument(
        "--base",
        default="HEAD",
        help="git ref to diff against (default: HEAD for uncommitted changes)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "github"],
        default="text",
        help="output format: 'text' (human) or 'github' (write to GITHUB_OUTPUT)",
    )
    parser.add_argument("files", nargs="*", help="Explicit .py paths (skips git diff)")
    args = parser.parse_args()

    if args.files:
        files = [Path(f) for f in args.files if Path(f).suffix == ".py"]
    else:
        files = _get_changed_files(args.base)

    if args.format == "text":
        if not files:
            print("No changed language server or solidlsp test files found.")
            return
        for filepath in files:
            p = _resolve_repo_path(Path(filepath))
            if not p.is_file():
                continue
            if _is_language_server_source(p):
                for class_name, language_id in extract_language_ids(p):
                    print(f"{filepath}: {class_name} -> {language_id}")
            elif _is_solidlsp_test_source(p):
                for language_id in sorted(extract_language_ids_from_test_file(p)):
                    print(f"{filepath}: pytest -> {language_id}")
        return

    # github format: write to GITHUB_OUTPUT
    lang_ids, test_paths = collect(files)
    any_changed = bool(lang_ids)

    output_file = os.environ.get("GITHUB_OUTPUT", "")
    lines = [
        f"language_ids={json.dumps(lang_ids)}",
        f"test_paths={' '.join(test_paths)}",
        f"any_changed={'true' if any_changed else 'false'}",
    ]

    if output_file:
        with open(output_file, "a") as f:
            f.write("\n".join(lines) + "\n")
    else:
        # fallback: print to stdout (useful for local testing)
        for line in lines:
            print(line)


if __name__ == "__main__":
    main()
