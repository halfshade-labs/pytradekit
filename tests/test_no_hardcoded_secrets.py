import ast
import re
from pathlib import Path
from typing import List, Tuple

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "pytradekit"
SENSITIVE_TARGET_PATTERN = re.compile(
    r"(?:^|_)(?:api_(?:key|secret)|secret_key|passphrase|access_key|private_key)"
    r"(?:$|_)"
)
PLACEHOLDER_MARKERS = (
    "changeme",
    "dummy",
    "example",
    "placeholder",
    "redacted",
)


def _get_target_name(target: ast.expr) -> str:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _is_sensitive_target(name: str) -> bool:
    normalized = name.lower()
    if normalized.endswith(("_env", "_env_name")):
        return False
    return SENSITIVE_TARGET_PATTERN.search(normalized) is not None


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return True
    if normalized.startswith(("your-", "your_", "test-", "test_", "<")):
        return True
    if any(marker in normalized for marker in PLACEHOLDER_MARKERS):
        return True
    return re.fullmatch(r"[x-z*_.-]{3,}", normalized) is not None


def _find_literal_credentials(source_path: Path) -> List[Tuple[int, str]]:
    tree = ast.parse(
        source_path.read_text(encoding="utf-8"),
        filename=str(source_path),
    )
    findings = []
    for node in ast.walk(tree):
        assignments = []
        if isinstance(node, ast.Assign):
            assignments = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign):
            assignments = [(node.target, node.value)]
        for target, value_node in assignments:
            target_name = _get_target_name(target)
            if not _is_sensitive_target(target_name):
                continue
            if not isinstance(value_node, ast.Constant):
                continue
            if not isinstance(value_node.value, str):
                continue
            if not _is_placeholder(value_node.value):
                findings.append((node.lineno, target_name))
    return findings


def test_package_has_no_literal_exchange_credentials():
    findings = []
    for source_path in sorted(PACKAGE_ROOT.rglob("*.py")):
        for line_number, target_name in _find_literal_credentials(source_path):
            relative_path = source_path.relative_to(PACKAGE_ROOT.parent)
            findings.append(f"{relative_path}:{line_number}:{target_name}")

    assert not findings, "Hardcoded credential assignments found: " + ", ".join(findings)


@pytest.mark.parametrize(
    "placeholder",
    ["", "xxx", "your-api-key", "test_secret", "<redacted>", "changeme"],
)
def test_placeholder_values_are_allowed(placeholder):
    assert _is_placeholder(placeholder)


def test_non_placeholder_literal_is_rejected():
    assert not _is_placeholder("opaque-production-value")
