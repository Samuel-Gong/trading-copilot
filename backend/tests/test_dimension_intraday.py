"""板块分时 (dimension-intraday) 纯函数测试。

夹具: snapshot 扩展配置 (所属概念) + kline_minute/kline_daily 分区,
验证等权口径、停牌 ffill、prev_close/首根基准与各降级状态。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from app.api import ext_data as ext_data_api
from app.api.ext_data import _dimension_intraday_compute
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, write_ext_parquet


def _mk_config() -> ExtConfig:
    return ExtConfig(id="ext_gn", label="测试概念", mode="snapshot", fields=[])


def _write_ext(data_dir: Path, values: dict[str, str]) -> None:
    """snapshot 扩展数据: symbol → 所属概念 标签串。"""
    cfg_dir = data_dir / "ext_data" / "ext_gn"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame({
        "symbol": list(values.keys()),
        "所属概念": list(values.values()),
    })
    df.write_parquet(cfg_dir / "part.parquet")


def _write_minute(data_dir: Path, day: str, rows: list[tuple[str, str, float]]) -> None:
    part = data_dir / "kline_minute" / f"date={day}" / "part.parquet"
    part.parent.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "datetime": [datetime.fromisoformat(r[1]) for r in rows],
            "close": [r[2] for r in rows],
        },
        schema_overrides={"datetime": pl.Datetime("us")},
    )
    df.write_parquet(part)


def _write_daily(data_dir: Path, day: str, closes: dict[str, float]) -> None:
    part = data_dir / "kline_daily" / f"date={day}" / "part.parquet"
    part.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": list(closes.keys()),
        "close": list(closes.values()),
    }).write_parquet(part)


def test_dimension_intraday_equal_weight_and_ffill(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_ext(data_dir, {"000001.SZ": "人工智能、芯片", "000002.SZ": "人工智能"})
    # 前收: 000001=10.0 (+5%/+6%/+4%), 000002=20.0, 600000 非成分股
    _write_daily(data_dir, "2026-08-27", {"000001.SZ": 10.0, "000002.SZ": 20.0, "600000.SH": 5.0})
    # 000002 在 09:32 无成交 (停牌分钟) → ffill 沿用 20.4
    _write_minute(data_dir, "2026-08-28", [
        ("000001.SZ", "2026-08-28T09:31:00", 10.5),
        ("000002.SZ", "2026-08-28T09:31:00", 20.4),
        ("600000.SH", "2026-08-28T09:31:00", 5.05),
        ("000001.SZ", "2026-08-28T09:32:00", 10.6),
        ("600000.SH", "2026-08-28T09:32:00", 5.10),
        ("000001.SZ", "2026-08-28T09:33:00", 10.4),
        ("000002.SZ", "2026-08-28T09:33:00", 20.8),
        ("600000.SH", "2026-08-28T09:33:00", 5.20),
    ])

    payload = _dimension_intraday_compute(_mk_config(), data_dir, "所属概念", "人工智能", None)

    assert payload["status"] == "ok"
    assert payload["date"] == "2026-08-28"
    assert payload["basis"] == "prev_close"
    assert payload["member_count"] == 2
    assert payload["members_with_minute"] == 2
    points = payload["points"]
    assert [p["time"] for p in points] == ["09:31", "09:32", "09:33"]
    # 09:31: 成分等权 (5% + 2%)/2 = 3.5%; 全市场 (5+2+1)/3 ≈ 2.667% (小数制)
    assert points[0]["sector"] == 0.035
    assert points[0]["market"] == 0.0267
    # 09:32: 000002 ffill 20.4 → (6% + 2%)/2 = 4.0% (无 ffill 会是 6.0%)
    assert points[1]["sector"] == 0.04
    assert points[1]["market"] == 0.04
    # 09:33: (4% + 4%)/2 = 4.0%
    assert points[2]["sector"] == 0.04


def test_dimension_intraday_tag_no_partial_match(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_ext(data_dir, {"000001.SZ": "人工智能体"})
    _write_minute(data_dir, "2026-08-28", [("000001.SZ", "2026-08-28T09:31:00", 10.5)])
    payload = _dimension_intraday_compute(_mk_config(), data_dir, "所属概念", "人工智能", None)
    assert payload["status"] == "empty"
    assert payload["reason"] == "no_members"


def test_dimension_intraday_no_minute_store(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_ext(data_dir, {"000001.SZ": "人工智能"})
    payload = _dimension_intraday_compute(_mk_config(), data_dir, "所属概念", "人工智能", None)
    assert payload["status"] == "no_data"
    assert payload["reason"] == "minute_missing"


def test_dimension_intraday_requested_date_absent(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_ext(data_dir, {"000001.SZ": "人工智能"})
    _write_minute(data_dir, "2026-08-28", [("000001.SZ", "2026-08-28T09:31:00", 10.5)])
    payload = _dimension_intraday_compute(_mk_config(), data_dir, "所属概念", "人工智能", "2026-08-27")
    assert payload["status"] == "no_data"
    assert payload["reason"] == "minute_missing"


def test_dimension_intraday_members_without_bars(tmp_path: Path) -> None:
    """成分股全是 ETF 等无分钟数据的标的 → empty/no_member_bars。"""
    data_dir = tmp_path / "data"
    _write_ext(data_dir, {"510050.SH": "人工智能"})
    _write_daily(data_dir, "2026-08-27", {"510050.SH": 3.0, "000001.SZ": 10.0})
    _write_minute(data_dir, "2026-08-28", [("000001.SZ", "2026-08-28T09:31:00", 10.5)])
    payload = _dimension_intraday_compute(_mk_config(), data_dir, "所属概念", "人工智能", None)
    assert payload["status"] == "empty"
    assert payload["reason"] == "no_member_bars"
    assert payload["member_count"] == 1


def test_dimension_intraday_first_close_basis(tmp_path: Path) -> None:
    """无前一交易日日K → 基准退化为当日首根 close, 曲线起点 ≈ 0。"""
    data_dir = tmp_path / "data"
    _write_ext(data_dir, {"000001.SZ": "人工智能"})
    _write_minute(data_dir, "2026-08-28", [
        ("000001.SZ", "2026-08-28T09:31:00", 10.0),
        ("000001.SZ", "2026-08-28T09:32:00", 10.3),
    ])
    payload = _dimension_intraday_compute(_mk_config(), data_dir, "所属概念", "人工智能", None)
    assert payload["status"] == "ok"
    assert payload["basis"] == "first_close"
    assert payload["points"][0]["sector"] == 0.0
    assert payload["points"][1]["sector"] == 0.03


def test_dimension_intraday_explicit_date_uses_that_partition(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_ext(data_dir, {"000001.SZ": "人工智能"})
    _write_daily(data_dir, "2026-08-26", {"000001.SZ": 10.0})
    _write_daily(data_dir, "2026-08-27", {"000001.SZ": 11.0})
    _write_minute(data_dir, "2026-08-27", [("000001.SZ", "2026-08-27T09:31:00", 10.5)])
    _write_minute(data_dir, "2026-08-28", [("000001.SZ", "2026-08-28T09:31:00", 12.1)])
    # 默认取最新分区 2026-08-28 → prev 为 08-27 的 11.0 → +10%
    latest = _dimension_intraday_compute(_mk_config(), data_dir, "所属概念", "人工智能", None)
    assert latest["date"] == "2026-08-28"
    assert latest["points"][0]["sector"] == 0.1
    # 显式指定 08-27 → prev 为 08-26 的 10.0 → +5%
    explicit = _dimension_intraday_compute(_mk_config(), data_dir, "所属概念", "人工智能", "2026-08-27")
    assert explicit["date"] == "2026-08-27"
    assert explicit["points"][0]["sector"] == 0.05


def test_dimension_intraday_cache_tracks_member_and_minute_generations(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = ExtConfigStore(data_dir)
    config = ExtConfig(
        id="ext_gn",
        label="测试概念",
        mode="snapshot",
        fields=[ExtField("symbol", "string"), ExtField("所属概念", "string")],
    )
    store.create(config)
    write_ext_parquet(
        pl.DataFrame({"symbol": ["000001.SZ", "000002.SZ"], "所属概念": ["人工智能", "其他"]}),
        config,
        data_dir,
    )
    _write_daily(data_dir, "2026-08-27", {"000001.SZ": 10.0, "000002.SZ": 20.0})
    _write_minute(data_dir, "2026-08-28", [
        ("000001.SZ", "2026-08-28T09:31:00", 11.0),
        ("000002.SZ", "2026-08-28T09:31:00", 24.0),
    ])
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(
            store=SimpleNamespace(data_dir=data_dir),
        )))
    )
    with ext_data_api._DIMENSION_INTRADAY_CACHE_LOCK:
        ext_data_api._DIMENSION_INTRADAY_CACHE.clear()

    first = ext_data_api.dimension_intraday(
        request, "ext_gn", "所属概念", "人工智能", None,
    )
    assert first["points"][0]["sector"] == 0.1

    current = store.get("ext_gn")
    assert current is not None
    write_ext_parquet(
        pl.DataFrame({"symbol": ["000001.SZ", "000002.SZ"], "所属概念": ["其他", "人工智能"]}),
        current,
        data_dir,
    )
    after_members = ext_data_api.dimension_intraday(
        request, "ext_gn", "所属概念", "人工智能", None,
    )
    assert after_members["points"][0]["sector"] == 0.2

    _write_minute(data_dir, "2026-08-28", [
        ("000001.SZ", "2026-08-28T09:31:00", 11.0),
        ("000002.SZ", "2026-08-28T09:31:00", 26.0),
    ])
    after_minute = ext_data_api.dimension_intraday(
        request, "ext_gn", "所属概念", "人工智能", None,
    )
    assert after_minute["points"][0]["sector"] == 0.3


def test_dimension_intraday_cache_is_bounded_and_expires(monkeypatch) -> None:
    monkeypatch.setattr(ext_data_api, "_DIMENSION_INTRADAY_CACHE_MAX_SIZE", 3)
    monkeypatch.setattr(ext_data_api, "_DIMENSION_INTRADAY_CACHE_TTL_S", 10.0)
    with ext_data_api._DIMENSION_INTRADAY_CACHE_LOCK:
        ext_data_api._DIMENSION_INTRADAY_CACHE.clear()

    for index in range(5):
        ext_data_api._put_dimension_intraday_cache(
            ("ext", "field", f"value-{index}", None),
            float(index),
            (index,),
            {"index": index},
        )

    with ext_data_api._DIMENSION_INTRADAY_CACHE_LOCK:
        assert len(ext_data_api._DIMENSION_INTRADAY_CACHE) == 3
        assert {key[2] for key in ext_data_api._DIMENSION_INTRADAY_CACHE} == {
            "value-2",
            "value-3",
            "value-4",
        }
        ext_data_api._prune_dimension_intraday_cache(20.0)
        assert ext_data_api._DIMENSION_INTRADAY_CACHE == {}
