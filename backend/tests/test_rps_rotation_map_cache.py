"""_load_concept_map_df 缓存契约回归 (#186)。

旧 bug: 正常路径返回 (map_df, count) 元组, 但缓存只存了裸 map_df,
600s 内第二次调用命中缓存返回 DataFrame, 调用方按元组解包会把两列
拆成两个 Series, 概念/行业分析二次访问必报错 (issue 截图定位)。
"""
from __future__ import annotations

import types

import pytest

from app.services import rps_rotation


@pytest.fixture(autouse=True)
def _clear_map_cache():
    rps_rotation.invalidate_cache()
    yield
    rps_rotation.invalidate_cache()


def _fake_repo(tmp_path):
    return types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path))


def _patch_ext(monkeypatch, rows: list[dict]) -> None:
    """替身 ext 配置读取: 免落盘, 聚焦缓存契约本身。"""
    config = types.SimpleNamespace(id="ext_gn_ths", mode="snapshot", fields=[])
    monkeypatch.setattr(rps_rotation.ExtConfigStore, "load_all", lambda self: [config])
    monkeypatch.setattr(
        rps_rotation, "_dimension_field",
        lambda cfg, kind: "所属概念" if kind == "concept" else None,
    )
    monkeypatch.setattr(rps_rotation, "_read_ext_rows", lambda data_dir, cfg, field: rows)
    monkeypatch.setattr(
        rps_rotation, "_symbol_keys", lambda row, cfg: [row["symbol"].upper()]
    )


def test_map_cache_hit_returns_same_tuple(tmp_path, monkeypatch):
    _patch_ext(monkeypatch, [
        {"symbol": "s1.SH", "所属概念": "人工智能"},
        {"symbol": "s2.SH", "所属概念": "芯片"},
    ])
    first = rps_rotation._load_concept_map_df(_fake_repo(tmp_path), "concept")
    assert isinstance(first, tuple) and len(first) == 2
    map_df, count = first
    assert count == 2
    assert sorted(map_df["_sym_up"].to_list()) == ["S1.SH", "S2.SH"]

    # 旧 bug: 命中缓存返回裸 DataFrame (只缓存了 map_df), 元组解包变两个 Series
    second = rps_rotation._load_concept_map_df(_fake_repo(tmp_path), "concept")
    assert isinstance(second, tuple) and len(second) == 2
    assert second[0].equals(map_df)
    assert second[1] == count


def test_map_cache_isolated_by_kind(tmp_path, monkeypatch):
    _patch_ext(monkeypatch, [
        {"symbol": "s1.SH", "所属概念": "人工智能"},
    ])
    repo = _fake_repo(tmp_path)
    concept = rps_rotation._load_concept_map_df(repo, "concept")
    industry = rps_rotation._load_concept_map_df(repo, "industry")
    assert concept[1] == 1
    assert industry[1] == 0
    assert industry[0].is_empty()


def test_future_membership_version_does_not_change_past_rps(tmp_path, monkeypatch):
    day1 = rps_rotation.date(2026, 8, 27)
    day2 = rps_rotation.date(2026, 8, 28)
    history = rps_rotation.pl.DataFrame({
        "symbol": ["S1.SH", "S2.SH", "S1.SH", "S2.SH"],
        "date": [day1, day1, day2, day2],
        "change_pct": [0.01, 0.50, 0.02, 0.20],
    })

    class Repo:
        store = types.SimpleNamespace(data_dir=tmp_path)
        _enriched_history_cache = history

        def get_enriched_history_snapshot(self):
            return "g1", history

        def get_matrix_data_generation(self, _asset_type):
            return "g1"

    map_df = rps_rotation.pl.DataFrame({
        "_source_id": ["source", "source"],
        "_effective_date": [day1, day2],
        "_sym_up": ["S1.SH", "S2.SH"],
        "concept": ["人工智能", "人工智能"],
    })
    monkeypatch.setattr(rps_rotation, "_ext_generation_signature", lambda *_args: "e1")
    monkeypatch.setattr(
        rps_rotation,
        "_load_concept_map_df",
        lambda *_args: (map_df, 1),
    )

    result = rps_rotation.build_rps_rotation(Repo(), days=7)

    assert result["columns"][str(day1)][0] == ("人工智能", 0.01)
    assert result["columns"][str(day2)][0] == ("人工智能", 0.20)


def test_ext_generation_change_bypasses_result_and_map_caches(tmp_path, monkeypatch):
    day = rps_rotation.date(2026, 8, 28)
    history = rps_rotation.pl.DataFrame({
        "symbol": ["S1.SH", "S2.SH"],
        "date": [day, day],
        "change_pct": [0.01, 0.20],
    })

    class Repo:
        store = types.SimpleNamespace(data_dir=tmp_path)
        _enriched_history_cache = history

        def get_enriched_history_snapshot(self):
            return "g1", history

        def get_matrix_data_generation(self, _asset_type):
            return "g1"

    generation = ["e1"]
    active_symbol = ["S1.SH"]

    def load_map(_repo, kind="concept"):
        return rps_rotation.pl.DataFrame({
            "_source_id": ["source"],
            "_effective_date": [day],
            "_sym_up": [active_symbol[0]],
            kind: ["人工智能"],
        }), 1

    monkeypatch.setattr(
        rps_rotation,
        "_ext_generation_signature",
        lambda *_args: generation[0],
    )
    monkeypatch.setattr(rps_rotation, "_load_concept_map_df", load_map)
    first = rps_rotation.build_rps_rotation(Repo(), days=7)
    generation[0] = "e2"
    active_symbol[0] = "S2.SH"
    second = rps_rotation.build_rps_rotation(Repo(), days=7)

    assert first["columns"][str(day)][0][1] == 0.01
    assert second["columns"][str(day)][0][1] == 0.20


def test_generation_changes_keep_rotation_caches_bounded(tmp_path, monkeypatch):
    day = rps_rotation.date(2026, 8, 28)
    history = rps_rotation.pl.DataFrame({
        "symbol": ["S1.SH"],
        "date": [day],
        "change_pct": [0.01],
    })

    class Repo:
        store = types.SimpleNamespace(data_dir=tmp_path)
        _enriched_history_cache = history

        def get_enriched_history_snapshot(self):
            return "g1", history

        def get_matrix_data_generation(self, _asset_type):
            return "g1"

    generation = ["e0"]
    monkeypatch.setattr(
        rps_rotation,
        "_ext_generation_signature",
        lambda *_args: generation[0],
    )
    monkeypatch.setattr(
        rps_rotation,
        "_read_ext_rows",
        lambda *_args, **_kwargs: [{"symbol": "S1.SH", "concept": "人工智能"}],
    )
    config = types.SimpleNamespace(id="source", mode="snapshot", fields=[])
    monkeypatch.setattr(rps_rotation.ExtConfigStore, "load_all", lambda _self: [config])
    monkeypatch.setattr(rps_rotation, "_dimension_field", lambda *_args: "concept")
    monkeypatch.setattr(rps_rotation, "_symbol_keys", lambda row, _cfg: [row["symbol"]])

    for index in range(rps_rotation._CACHE_MAX_ENTRIES + 5):
        generation[0] = f"e{index}"
        rps_rotation.build_rps_rotation(Repo(), days=7)

    assert len(rps_rotation._map_cache) == 1
    assert len(rps_rotation._cache) <= rps_rotation._CACHE_MAX_ENTRIES
    assert set(rps_rotation._cache) == set(rps_rotation._cache_ts)
