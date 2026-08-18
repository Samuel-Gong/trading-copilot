#!/usr/bin/env python3
"""从 GitHub Tags API 输出中选择最新的 upstream 版本 Tag。"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SUPPORTED_TAG_PATTERN = r"^v?(\d+)\.(\d+)\.(\d+)$"
SUPPORTED_UPSTREAM = "shy3130/tickflow-stock-panel"


class ReleaseSelectionError(ValueError):
    """输入或同步状态不可信, 必须停止自动同步。"""


@dataclass(frozen=True)
class Release:
    tag: str
    commit: str
    version: tuple[int, int, int]


def _require_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseSelectionError(f"{context} 必须是 JSON object")
    return value


def _parse_commit(value: Any, context: str) -> str:
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise ReleaseSelectionError(f"{context} 必须是 40 位小写 commit SHA")
    return value


def _compile_tag_pattern(state: dict[str, Any]) -> re.Pattern[str]:
    pattern = state.get("tag_pattern")
    if not isinstance(pattern, str):
        raise ReleaseSelectionError("tag_pattern 必须是字符串")
    if pattern != SUPPORTED_TAG_PATTERN:
        raise ReleaseSelectionError("tag_pattern 不是自动化支持的固定版本规则")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ReleaseSelectionError(f"tag_pattern 无效: {exc}") from exc
    if compiled.groups != 3:
        raise ReleaseSelectionError("tag_pattern 必须恰好捕获主、次、补丁三个版本号")
    return compiled


def _parse_release(tag: Any, commit: Any, pattern: re.Pattern[str]) -> Release | None:
    if not isinstance(tag, str):
        return None
    match = pattern.fullmatch(tag)
    if match is None:
        return None
    parsed_commit = _parse_commit(commit, f"Tag {tag} 的 commit")
    return Release(
        tag=tag,
        commit=parsed_commit,
        version=tuple(int(part) for part in match.groups()),
    )


def _flatten_tag_pages(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ReleaseSelectionError("Tags API 输出必须是 JSON array")

    flattened: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, list):
            for nested in item:
                flattened.append(_require_mapping(nested, "Tag 条目"))
        else:
            flattened.append(_require_mapping(item, "Tag 条目"))
    return flattened


def _parse_releases(tags_payload: Any, pattern: re.Pattern[str]) -> list[Release]:
    releases: list[Release] = []
    for item in _flatten_tag_pages(tags_payload):
        commit = item.get("commit")
        commit_sha = commit.get("sha") if isinstance(commit, dict) else None
        release = _parse_release(item.get("name"), commit_sha, pattern)
        if release is not None:
            releases.append(release)

    if not releases:
        raise ReleaseSelectionError("upstream 没有符合规则的版本 Tag")
    _validate_equivalent_tag_commits(releases)
    return releases


def _validate_equivalent_tag_commits(releases: list[Release]) -> None:
    by_version: dict[tuple[int, int, int], list[Release]] = {}
    for release in releases:
        by_version.setdefault(release.version, []).append(release)

    for equivalent_tags in by_version.values():
        if len({release.commit for release in equivalent_tags}) > 1:
            tags = ", ".join(sorted(release.tag for release in equivalent_tags))
            raise ReleaseSelectionError(f"同一版本的 Tag 指向不同 commit: {tags}")


def _select_latest(releases: list[Release]) -> Release:
    latest_version = max(release.version for release in releases)
    latest = [release for release in releases if release.version == latest_version]

    # 同一版本同时存在 v 前缀与无前缀 Tag 时, 稳定地优先选择 v 前缀。
    return min(latest, key=lambda release: (not release.tag.startswith("v"), release.tag))


def select_release(tags_payload: Any, state_payload: Any) -> dict[str, str]:
    state = _require_mapping(state_payload, "同步状态")
    if state.get("schema_version") != 1:
        raise ReleaseSelectionError("不支持的同步状态 schema_version")
    if state.get("upstream_repository") != SUPPORTED_UPSTREAM:
        raise ReleaseSelectionError("upstream_repository 与受信任仓库不一致")
    pattern = _compile_tag_pattern(state)
    _parse_commit(state.get("baseline_commit"), "baseline_commit")
    _parse_commit(state.get("merge_base_commit"), "merge_base_commit")

    previous_payload = _require_mapping(
        state.get("last_synced_release"), "last_synced_release"
    )
    previous = _parse_release(
        previous_payload.get("tag"), previous_payload.get("commit"), pattern
    )
    if previous is None:
        raise ReleaseSelectionError("last_synced_release.tag 不符合 tag_pattern")

    releases = _parse_releases(tags_payload, pattern)
    latest = _select_latest(releases)
    result = {
        "tag": latest.tag,
        "commit": latest.commit,
        "version": ".".join(str(part) for part in latest.version),
        "previous_tag": previous.tag,
        "previous_commit": previous.commit,
    }

    previous_tags = [release for release in releases if release.tag == previous.tag]
    if not previous_tags:
        return {
            "status": "tag_missing",
            "tag": previous.tag,
            "commit": previous.commit,
            "version": ".".join(str(part) for part in previous.version),
            "previous_tag": previous.tag,
            "previous_commit": previous.commit,
        }

    if latest.version < previous.version:
        raise ReleaseSelectionError("同步状态版本高于 upstream 最新版本")

    current_previous_commit = previous_tags[0].commit
    if current_previous_commit != previous.commit:
        return {
            "status": "retagged",
            "tag": previous.tag,
            "commit": current_previous_commit,
            "version": ".".join(str(part) for part in previous.version),
            "previous_tag": previous.tag,
            "previous_commit": previous.commit,
        }

    result["status"] = (
        "no_change" if latest.version == previous.version else "new_release"
    )
    return result


def _write_github_output(path: Path, result: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for key, value in result.items():
            if "\n" in value or "\r" in value:
                raise ReleaseSelectionError(f"GitHub output {key} 不能包含换行")
            output.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--tags", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    with args.state.open(encoding="utf-8") as state_file:
        state_payload = json.load(state_file)
    with args.tags.open(encoding="utf-8") as tags_file:
        tags_payload = json.load(tags_file)

    result = select_release(tags_payload, state_payload)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.github_output is not None:
        _write_github_output(args.github_output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
