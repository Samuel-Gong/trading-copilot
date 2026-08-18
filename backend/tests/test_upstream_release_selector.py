from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).parents[2] / ".github" / "scripts" / "select_upstream_release.py"
)
SPEC = importlib.util.spec_from_file_location("select_upstream_release", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

COMMIT_88 = "8ead30037a8806518e400dc26b67a7e5a1294282"
COMMIT_89 = "1111111111111111111111111111111111111111"


def _state(tag: str = "v0.1.88", commit: str = COMMIT_88) -> dict[str, object]:
    return {
        "schema_version": 1,
        "upstream_repository": "shy3130/tickflow-stock-panel",
        "tag_pattern": r"^v?(\d+)\.(\d+)\.(\d+)$",
        "baseline_commit": "99bdec875d4bbf5c30ddc43534b81ada0f3b0f6b",
        "merge_base_commit": "99bdec875d4bbf5c30ddc43534b81ada0f3b0f6b",
        "last_synced_release": {"tag": tag, "commit": commit},
    }


def _tag(name: str, commit: str) -> dict[str, object]:
    return {"name": name, "commit": {"sha": commit}}


def test_selects_latest_semantic_version_from_paginated_payload() -> None:
    payload = [
        [_tag("v0.1.88", COMMIT_88), _tag("nightly", "not-a-sha")],
        [_tag("v0.1.89", COMMIT_89)],
    ]

    result = MODULE.select_release(payload, _state())

    assert result == {
        "status": "new_release",
        "tag": "v0.1.89",
        "commit": COMMIT_89,
        "version": "0.1.89",
        "previous_tag": "v0.1.88",
        "previous_commit": COMMIT_88,
    }


def test_prefers_v_prefix_when_equivalent_tags_share_commit() -> None:
    payload = [
        _tag("v0.1.88", COMMIT_88),
        _tag("0.1.89", COMMIT_89),
        _tag("v0.1.89", COMMIT_89),
    ]

    result = MODULE.select_release(payload, _state())

    assert result["tag"] == "v0.1.89"


def test_reports_no_change_for_same_tag_and_commit() -> None:
    result = MODULE.select_release([_tag("v0.1.88", COMMIT_88)], _state())

    assert result["status"] == "no_change"


def test_reports_retagged_when_synced_version_changes_commit() -> None:
    result = MODULE.select_release(
        [_tag("v0.1.88", COMMIT_89), _tag("v0.1.89", "2" * 40)], _state()
    )

    assert result["status"] == "retagged"
    assert result["tag"] == "v0.1.88"
    assert result["commit"] == COMMIT_89


def test_rejects_equivalent_tags_that_point_to_different_commits() -> None:
    payload = [_tag("0.1.89", COMMIT_88), _tag("v0.1.89", COMMIT_89)]

    with pytest.raises(MODULE.ReleaseSelectionError, match="指向不同 commit"):
        MODULE.select_release(payload, _state())


def test_rejects_older_equivalent_tags_that_point_to_different_commits() -> None:
    payload = [
        _tag("v0.1.88", COMMIT_88),
        _tag("0.1.88", "2" * 40),
        _tag("v0.1.89", COMMIT_89),
    ]

    with pytest.raises(MODULE.ReleaseSelectionError, match="指向不同 commit"):
        MODULE.select_release(payload, _state())


def test_reports_deleted_synced_tag_when_only_older_versions_remain() -> None:
    result = MODULE.select_release([_tag("v0.1.87", "3" * 40)], _state())

    assert result["status"] == "tag_missing"
    assert result["tag"] == "v0.1.88"
    assert result["commit"] == COMMIT_88


def test_reports_deleted_synced_tag() -> None:
    result = MODULE.select_release([_tag("v0.1.89", COMMIT_89)], _state())

    assert result["status"] == "tag_missing"
    assert result["tag"] == "v0.1.88"
    assert result["commit"] == COMMIT_88


def test_rejects_invalid_commit_for_matching_release_tag() -> None:
    with pytest.raises(MODULE.ReleaseSelectionError, match="40 位小写 commit SHA"):
        MODULE.select_release([_tag("v0.1.89", "not-a-sha")], _state())


def test_rejects_untrusted_upstream_repository() -> None:
    state = _state()
    state["upstream_repository"] = "attacker/example"

    with pytest.raises(MODULE.ReleaseSelectionError, match="受信任仓库不一致"):
        MODULE.select_release([_tag("v0.1.88", COMMIT_88)], state)
