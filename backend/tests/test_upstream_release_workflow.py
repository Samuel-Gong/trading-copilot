from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JQ = shutil.which("jq")
FILTER_PATH = (
    Path(__file__).parents[2] / ".github" / "scripts" / "trusted_upstream_issue.jq"
)
MARKER = (
    "<!-- upstream-sync:new_release:shy3130/tickflow-stock-panel:"
    "v0.1.89:1111111111111111111111111111111111111111 -->"
)
EXPECTED_TITLE = "[Upstream] 同步 v0.1.89"

pytestmark = pytest.mark.skipif(JQ is None, reason="需要 jq 验证 workflow 去重过滤器")


def _matches(issues: list[dict[str, object]]) -> bool:
    assert JQ is not None
    result = subprocess.run(
        [
            JQ,
            "-e",
            "--arg",
            "marker",
            MARKER,
            "--arg",
            "expected_title",
            EXPECTED_TITLE,
            "-f",
            str(FILTER_PATH),
        ],
        input=json.dumps([issues]),
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode not in {0, 1}:
        raise AssertionError(result.stderr)
    return result.returncode == 0


def _issue(
    login: str,
    *,
    title: str = EXPECTED_TITLE,
    body: str = MARKER,
    pull_request: bool = False,
) -> dict[str, object]:
    issue: dict[str, object] = {
        "user": {"login": login},
        "title": title,
        "body": body,
    }
    if pull_request:
        issue["pull_request"] = {"url": "https://api.github.com/example"}
    return issue


def test_dedupes_only_trusted_tracking_issue() -> None:
    assert not _matches([_issue("attacker")])
    assert not _matches([_issue("github-actions[bot]", pull_request=True)])
    assert not _matches(
        [_issue("github-actions[bot]", title="[Upstream 安全告警] v0.1.89")]
    )
    assert _matches([_issue("github-actions[bot]")])
