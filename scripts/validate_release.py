#!/usr/bin/env python3
"""Validate the CouchMate Core Dev Preview release contract.

This script deliberately uses only the Python standard library. It validates
the package without importing Home Assistant, so it can run in a clean CI
checkout and cannot create ``__pycache__`` files.
"""

from __future__ import annotations

import ast
from collections import Counter
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "custom_components"
PACKAGE = COMPONENTS / "couchmate"
EXPECTED_VERSION = "1.4.0-beta.1"
EXPECTED_BRAND = "CouchMate Core Dev Preview"
FORBIDDEN_NAMESPACES = ("couchmate_dev", "couchmate-dev")

REQUIRED_V1_VIEWS = {
    "CouchMateEntitiesView": (
        "/api/couchmate/entities",
        "api:couchmate:entities",
    ),
    "CouchMateInfoView": (
        "/api/couchmate/info",
        "api:couchmate:info",
    ),
    "PairingCreateView": (
        "/api/couchmate/pairing/create",
        "api:couchmate:pairing:create",
    ),
    "PairingStatusView": (
        "/api/couchmate/pairing/status",
        "api:couchmate:pairing:status",
    ),
    "PairingApproveView": (
        "/api/couchmate/pairing/approve",
        "api:couchmate:pairing:approve",
    ),
    "PairingExchangeView": (
        "/api/couchmate/pairing/exchange",
        "api:couchmate:pairing:exchange",
    ),
    "PairingCancelView": (
        "/api/couchmate/pairing/cancel",
        "api:couchmate:pairing:cancel",
    ),
    "CouchMateClientInfoView": (
        "/api/couchmate/client/info",
        "api:couchmate:client:info",
    ),
    "CouchMateClientEntitiesView": (
        "/api/couchmate/client/entities",
        "api:couchmate:client:entities",
    ),
    "CouchMateClientServiceView": (
        "/api/couchmate/client/service",
        "api:couchmate:client:service",
    ),
}


class ValidationError(Exception):
    """Raised when the release contract is violated."""


def require(condition: bool, message: str) -> None:
    """Raise a readable validation error for a failed invariant."""
    if not condition:
        raise ValidationError(message)


def relative(path: Path) -> str:
    """Return a stable repository-relative path for diagnostics."""
    return path.relative_to(ROOT).as_posix()


def source_files() -> list[Path]:
    """Return text files that form the installed runtime package."""
    extensions = {".json", ".py", ".yaml", ".yml"}
    return sorted(
        path
        for path in PACKAGE.rglob("*")
        if path.is_file() and path.suffix.lower() in extensions
    )


def module_constants(path: Path) -> dict[str, Any]:
    """Read literal module-level assignments without importing the module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if value is None:
            continue
        try:
            literal = ast.literal_eval(value)
        except (TypeError, ValueError):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                values[target.id] = literal
    return values


def check_layout_and_metadata() -> None:
    """Verify HACS installs exactly one canonical-domain package."""
    require(PACKAGE.is_dir(), "missing custom_components/couchmate package")
    require(
        not (COMPONENTS / "couchmate_dev").exists(),
        "legacy custom_components/couchmate_dev package must not be shipped",
    )
    component_dirs = sorted(
        path.name for path in COMPONENTS.iterdir() if path.is_dir()
    )
    require(
        component_dirs == ["couchmate"],
        f"unexpected custom component directories: {component_dirs}",
    )

    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    require(manifest.get("domain") == "couchmate", "manifest domain must be couchmate")
    require(manifest.get("name") == EXPECTED_BRAND, "manifest must keep Dev Preview branding")
    require(manifest.get("version") == EXPECTED_VERSION, "manifest version is inconsistent")
    require(hacs.get("name") == EXPECTED_BRAND, "hacs.json must keep Dev Preview branding")
    require(hacs.get("content_in_root") is False, "HACS content_in_root must remain false")

    constants = module_constants(PACKAGE / "const.py")
    expected_constants = {
        "DOMAIN": "couchmate",
        "STORAGE_KEY": "couchmate",
        "PAIRING_CLIENT_STORAGE_KEY": "couchmate.paired_clients",
        "CONFIGURATION_STORAGE_KEY": "couchmate.configuration",
        "BACKGROUND_DIRECTORY": "couchmate/backgrounds",
    }
    for key, value in expected_constants.items():
        require(constants.get(key) == value, f"{key} must be {value!r}")

    init_constants = module_constants(PACKAGE / "__init__.py")
    require(init_constants.get("PANEL_URL_PATH") == "couchmate", "panel path must be couchmate")
    require(
        init_constants.get("PANEL_CONFIGURATOR_URL") == "/couchmate/configurator",
        "panel configurator URL must use the canonical namespace",
    )

    init_source = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    require(
        f'sidebar_title="{EXPECTED_BRAND}"' in init_source,
        "Home Assistant sidebar must keep Dev Preview branding",
    )
    for path in (
        PACKAGE / "strings.json",
        PACKAGE / "translations" / "de.json",
        PACKAGE / "translations" / "en.json",
    ):
        require(
            EXPECTED_BRAND in path.read_text(encoding="utf-8"),
            f"{relative(path)} must expose Dev Preview branding",
        )


def check_syntax_and_artifacts() -> None:
    """Parse every JSON/Python source and reject generated Python artifacts."""
    json_paths = sorted([ROOT / "hacs.json", *PACKAGE.rglob("*.json")])
    for path in json_paths:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as err:
            raise ValidationError(f"invalid JSON in {relative(path)}: {err}") from err

    python_paths = sorted(PACKAGE.rglob("*.py"))
    require(bool(python_paths), "no Python package sources found")
    for path in python_paths:
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec", dont_inherit=True)
        except (OSError, UnicodeError, SyntaxError) as err:
            raise ValidationError(f"invalid Python in {relative(path)}: {err}") from err

    generated = sorted(
        relative(path)
        for path in ROOT.rglob("*")
        if ".git" not in path.parts
        and (path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"})
    )
    require(not generated, f"generated Python artifacts must not be shipped: {generated}")


def check_namespace_and_versions() -> None:
    """Reject split runtime namespaces and inconsistent release versions."""
    runtime_files = source_files()
    require(bool(runtime_files), "runtime package contains no source files")
    for path in runtime_files:
        text = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_NAMESPACES:
            require(
                forbidden not in text,
                f"legacy runtime namespace {forbidden!r} found in {relative(path)}",
            )

    version_pattern = re.compile(r"\b\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?\b")
    occurrences: list[tuple[str, str]] = []
    for path in runtime_files:
        for match in version_pattern.finditer(path.read_text(encoding="utf-8")):
            occurrences.append((relative(path), match.group(0)))
    require(bool(occurrences), "runtime package exposes no release version")
    inconsistent = sorted(
        f"{path}: {version}" for path, version in occurrences if version != EXPECTED_VERSION
    )
    require(not inconsistent, f"inconsistent runtime versions: {inconsistent}")
    require(
        Counter(version for _, version in occurrences)[EXPECTED_VERSION] == 4,
        "release version must appear in manifest, both info responses, and sensor metadata",
    )


def class_literal(node: ast.ClassDef, name: str) -> str | None:
    """Return one string literal assigned in a class body."""
    for item in node.body:
        if not isinstance(item, ast.Assign) or len(item.targets) != 1:
            continue
        target = item.targets[0]
        if isinstance(target, ast.Name) and target.id == name:
            try:
                value = ast.literal_eval(item.value)
            except (TypeError, ValueError):
                return None
            return value if isinstance(value, str) else None
    return None


def check_http_views() -> None:
    """Verify canonical, unique and registered Home Assistant HTTP views."""
    views: dict[str, tuple[str, str, str]] = {}
    trees: dict[Path, ast.AST] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        trees[path] = tree
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            base_names = {
                base.id
                for base in node.bases
                if isinstance(base, ast.Name)
            }
            if "HomeAssistantView" not in base_names:
                continue
            url = class_literal(node, "url")
            name = class_literal(node, "name")
            require(url is not None, f"{node.name} has no literal URL")
            require(name is not None, f"{node.name} has no literal name")
            require(node.name not in views, f"duplicate HTTP view class {node.name}")
            views[node.name] = (url, name, relative(path))

    require(len(views) == 29, f"expected 29 HTTP views, found {len(views)}")
    urls = [item[0] for item in views.values()]
    names = [item[1] for item in views.values()]
    duplicate_urls = sorted(value for value, count in Counter(urls).items() if count > 1)
    duplicate_names = sorted(value for value, count in Counter(names).items() if count > 1)
    require(not duplicate_urls, f"duplicate HTTP view URLs: {duplicate_urls}")
    require(not duplicate_names, f"duplicate HTTP view names: {duplicate_names}")

    for class_name, (url, name, path) in views.items():
        require(
            url in {"/couchmate/configurator", "/couchmate/management"}
            or url.startswith("/api/couchmate/"),
            f"non-canonical URL in {path}:{class_name}: {url}",
        )
        require(
            name.startswith("api:couchmate:") or name.startswith("couchmate:"),
            f"non-canonical view name in {path}:{class_name}: {name}",
        )

    for class_name, expected in REQUIRED_V1_VIEWS.items():
        actual = views.get(class_name)
        require(actual is not None, f"required v1 view {class_name} is missing")
        require(
            actual[:2] == expected,
            f"required v1 view {class_name} changed: expected {expected}, found {actual[:2]}",
        )

    registrations: Counter[str] = Counter()
    view_names = set(views)
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id in view_names:
                registrations[node.func.id] += 1
    missing = sorted(view_names - set(registrations))
    repeated = sorted(name for name, count in registrations.items() if count != 1)
    require(not missing, f"HTTP views not instantiated for registration: {missing}")
    require(not repeated, f"HTTP views instantiated more than once: {repeated}")


def check_readme_migration_scope() -> None:
    """Allow old namespace terms only in explicit migration warnings."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required_guidance = (
        "darf nicht parallel dazu installiert werden",
        "must not be installed alongside it",
        "couchmate.uninstall",
        "vollständig neu",
        "full Home Assistant restart",
    )
    for phrase in required_guidance:
        require(phrase in readme, f"README is missing switching guidance: {phrase!r}")

    migration_headings = {
        "## Einmaliger Wechsel von der früheren parallelen Dev Preview",
        "## One-time migration from the former parallel Dev Preview",
    }
    current_heading = ""
    violations: list[str] = []
    for number, line in enumerate(readme.splitlines(), start=1):
        if line.startswith("## "):
            current_heading = line
        if not any(forbidden in line for forbidden in FORBIDDEN_NAMESPACES):
            continue
        is_release_warning = line.startswith("> ") and "1.3.0-beta.3" in line
        if current_heading not in migration_headings and not is_release_warning:
            violations.append(f"README.md:{number}")
    require(
        not violations,
        "legacy namespace terms are allowed only in migration guidance: "
        + ", ".join(violations),
    )


def main() -> int:
    """Run all release checks with one concise CI result."""
    checks = (
        check_layout_and_metadata,
        check_syntax_and_artifacts,
        check_namespace_and_versions,
        check_http_views,
        check_readme_migration_scope,
    )
    try:
        for check in checks:
            check()
    except ValidationError as err:
        print(f"release validation: FAIL\n{err}", file=sys.stderr)
        return 1
    print(
        "release validation: PASS "
        f"({EXPECTED_BRAND}, {EXPECTED_VERSION}, 29 unique HTTP views)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
