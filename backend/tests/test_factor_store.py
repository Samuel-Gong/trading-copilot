"""自定义/复合因子存储与 scoring 桥测试 (P3)。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.factors import store
from app.factors.registry import (
    FactorSpec,
    all_factors,
    factor_columns_view,
    get_factor,
    unregister_factor,
)
from app.strategy import scoring


@pytest.fixture()
def cleanup_registry():
    """测试注册的自定义因子在用例后清理, 不污染全局注册表。"""
    before = set()
    yield before
    for fid in before:
        unregister_factor(fid)


def _panel(n_days: int = 30) -> pl.DataFrame:
    rows = []
    volumes = {"A": 1000.0, "B": 3000.0, "C": 2000.0}
    for index in range(n_days):
        for symbol, close in (("A", 10.0 + index), ("B", 50.0 - index), ("C", 20.0 + index * 2)):
            rows.append({
                "symbol": symbol, "date": date(2026, 1, index + 1),
                "close": close, "volume": volumes[symbol] + index, "amount": (1000.0 + index) * close,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def test_custom_factor_definition_roundtrip(tmp_path, cleanup_registry) -> None:
    definition = {
        "id": "uf_test_rev", "kind": "custom", "version": 1, "label": "测试反转",
        "group": "自定义", "formula": "rank(-ts_sum(close / ts_delay(close, 1) - 1, 5))",
        "description": "", "direction": "low", "status": "draft",
    }
    spec = store.register_definition(definition)
    cleanup_registry.add("uf_test_rev")
    assert spec.kind == "custom"
    assert "close" in spec.dependencies
    assert spec.warmup_bars >= 6

    store.save_one(tmp_path, definition)
    loaded = store.load_all(tmp_path)
    assert len(loaded) == 1 and loaded[0]["id"] == "uf_test_rev"

    # 目录视图与 all_factors 追加动态因子
    ids = [item["id"] for item in factor_columns_view()]
    assert ids[:77] == [item["id"] for item in factor_columns_view()[:77]]
    assert "uf_test_rev" in ids and ids.index("uf_test_rev") >= 77
    assert any(s.id == "uf_test_rev" for s in all_factors())

    # 快照约束不受影响: 未注册动态因子时目录 = 77 内置
    unregister_factor("uf_test_rev")
    assert len(factor_columns_view()) == 77


def test_custom_factor_invalid_rejected(cleanup_registry) -> None:
    with pytest.raises(ValueError, match="E005"):
        store.register_definition({
            "id": "uf_bad", "kind": "custom", "label": "坏因子",
            "formula": "ts_delay(close, -3)", "status": "draft",
        })
    with pytest.raises(ValueError, match="uf_"):
        store.register_definition({
            "id": "wrong_prefix", "kind": "custom", "label": "坏前缀",
            "formula": "close", "status": "draft",
        })


def test_composite_definition_and_cycle_guard(cleanup_registry) -> None:
    definition = {
        "id": "cf_test_combo", "kind": "composite", "version": 1, "label": "测试组合",
        "members": {"momentum_20d": 0.6, "turnover_rate": 0.4}, "status": "draft",
    }
    spec = store.register_definition(definition)
    cleanup_registry.add("cf_test_combo")
    assert spec.kind == "composite"
    assert spec.components == (("momentum_20d", 0.6), ("turnover_rate", 0.4))
    assert spec.dependencies == frozenset({"momentum_20d", "turnover_rate"})

    # 自引用拒绝
    with pytest.raises(ValueError, match="自身"):
        store.to_spec({**definition, "id": "cf_self", "members": {"cf_self": 1.0, "close": 1.0}})


def test_scoring_bridge_composite(cleanup_registry) -> None:
    """复合因子经 scoring 物化: 截面加权 z 分可计算且依赖展开正确。"""
    store.register_definition({
        "id": "cf_ztest", "kind": "composite", "version": 1, "label": "桥接测试",
        "members": {"close": 0.5, "volume": 0.5}, "status": "active",
    })
    cleanup_registry.add("cf_ztest")

    deps = scoring.scoring_dependencies({"cf_ztest": 1.0})
    assert deps == {"close", "volume"}
    assert scoring.scoring_warmup_bars({"cf_ztest": 1.0}) >= 1

    frame = scoring.materialize_scoring_columns(_panel(), {"cf_ztest"})
    assert "cf_ztest" in frame.columns
    values = frame.filter(pl.col("date") == date(2026, 1, 10))["cf_ztest"]
    assert values.is_not_null().all()
    # 截面 z 之和的均值近似为 0 (等权两成员)
    assert abs(values.mean()) < 1e-9


def test_scoring_bridge_custom_materializes(cleanup_registry) -> None:
    """自定义 DSL 因子经 materialize_scoring_columns 物化 (与检验共用路径)。"""
    store.register_definition({
        "id": "uf_rank_close", "kind": "custom", "version": 1, "label": "价格排名",
        "formula": "rank(close)", "status": "draft",
    })
    cleanup_registry.add("uf_rank_close")
    frame = scoring.materialize_scoring_columns(_panel(), {"uf_rank_close"})
    assert "uf_rank_close" in frame.columns
    day = frame.filter(pl.col("date") == date(2026, 1, 1))
    assert day["uf_rank_close"].is_not_null().all()


def test_composite_recursively_materializes_custom_and_nested_members(
    cleanup_registry,
) -> None:
    """复合因子必须按拓扑顺序先算 custom 与内层 composite。"""
    definitions = [
        {
            "id": "uf_nested_close",
            "kind": "custom",
            "version": 1,
            "label": "自定义价格",
            "formula": "rank(close)",
            "status": "draft",
        },
        {
            "id": "cf_nested_inner",
            "kind": "composite",
            "version": 1,
            "label": "内层组合",
            "members": {"uf_nested_close": 1.0, "volume": 1.0},
            "status": "draft",
        },
        {
            "id": "cf_nested_outer",
            "kind": "composite",
            "version": 1,
            "label": "外层组合",
            "members": {"cf_nested_inner": 1.0, "amount": 1.0},
            "status": "draft",
        },
    ]
    for definition in definitions:
        store.register_definition(definition)
        cleanup_registry.add(definition["id"])

    frame = scoring.materialize_scoring_columns(_panel(), {"cf_nested_outer"})

    assert {
        "uf_nested_close",
        "cf_nested_inner",
        "cf_nested_outer",
    }.issubset(frame.columns)
    assert frame["cf_nested_outer"].is_not_null().any()


def test_custom_and_composite_propagate_financial_asset_and_pit_scope(
    cleanup_registry,
) -> None:
    custom = store.register_definition({
        "id": "uf_financial_scope",
        "kind": "custom",
        "version": 1,
        "label": "财务派生",
        "formula": "roe_latest + pb_latest",
        "status": "draft",
    })
    cleanup_registry.add(custom.id)
    composite = store.register_definition({
        "id": "cf_financial_scope",
        "kind": "composite",
        "version": 1,
        "label": "财务组合",
        "members": {"uf_financial_scope": 1.0, "close": 1.0},
        "status": "draft",
    })
    cleanup_registry.add(composite.id)

    for spec in (custom, composite):
        assert spec.asset_types == frozenset({"stock"})
        assert spec.pit is True
        assert spec.pit_source == "financial_announce"
    etf_ids = {spec.id for spec in all_factors(asset_type="etf")}
    assert custom.id not in etf_ids
    assert composite.id not in etf_ids


def test_load_into_registry_isolated_failure(tmp_path, cleanup_registry) -> None:
    good = {
        "id": "uf_good", "kind": "custom", "version": 1, "label": "好因子",
        "formula": "close + 1", "status": "draft",
    }
    store.save_one(tmp_path, good)
    (tmp_path / "user_data" / "custom_factors" / "uf_broken.json").write_text(
        "{ not json", encoding="utf-8"
    )
    loaded = store.load_into_registry(tmp_path)
    assert loaded == ["uf_good"]
    cleanup_registry.add("uf_good")


def test_load_into_registry_restores_composite_chain_deeper_than_three_rounds(
    tmp_path, cleanup_registry,
) -> None:
    """合法深层组合链重启后应持续重试到全部注册。"""
    definitions = [{
        "id": "uf_deep_leaf",
        "kind": "custom",
        "version": 1,
        "label": "深层叶子",
        "formula": "close",
        "status": "draft",
    }]
    for depth in reversed(range(5)):
        child = "uf_deep_leaf" if depth == 4 else f"cf_deep_{depth + 1}"
        definitions.append({
            "id": f"cf_deep_{depth}",
            "kind": "composite",
            "version": 1,
            "label": f"深层组合 {depth}",
            "members": {child: 1.0, "volume": 1.0},
            "status": "draft",
        })
    for definition in definitions:
        store.save_one(tmp_path, definition)
        cleanup_registry.add(definition["id"])

    loaded = store.load_into_registry(tmp_path)

    assert set(loaded) == {definition["id"] for definition in definitions}
    assert get_factor("cf_deep_0") is not None


def test_unregister_builtin_rejected() -> None:
    with pytest.raises(ValueError, match="内置"):
        unregister_factor("rsi_14")
    spec = get_factor("rsi_14")
    assert isinstance(spec, FactorSpec)


def test_atomic_save_preserves_old_file_when_replace_fails(tmp_path, monkeypatch) -> None:
    """原子替换失败时旧定义完整保留, 不留下半截 JSON。"""
    original = {
        "id": "uf_atomic", "kind": "custom", "version": 1, "label": "旧定义",
        "formula": "close", "status": "draft",
    }
    store.save_one(tmp_path, original)

    def _fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(store.os, "replace", _fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.save_one(tmp_path, {**original, "label": "新定义"})

    assert store.load_all(tmp_path) == [original]


def test_persist_definition_writes_before_registry_mutation(
    tmp_path, monkeypatch, cleanup_registry,
) -> None:
    """落盘失败时不得把新因子留在进程注册表。"""
    definition = {
        "id": "uf_write_fail", "kind": "custom", "version": 1, "label": "写失败",
        "formula": "close", "status": "draft",
    }
    cleanup_registry.add("uf_write_fail")

    def _fail_save(_data_dir, _value):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_one", _fail_save)
    with pytest.raises(OSError, match="disk full"):
        store.persist_definition(tmp_path, definition)

    assert get_factor("uf_write_fail") is None


def test_persist_definition_rolls_back_file_when_registration_fails(
    tmp_path, cleanup_registry,
) -> None:
    """注册表拒绝新定义时恢复旧文件, 保持磁盘与内存一致。"""
    original = {
        "id": "uf_register_fail",
        "kind": "custom",
        "version": 1,
        "label": "旧定义",
        "formula": "close",
        "status": "draft",
    }
    store.save_one(tmp_path, original)
    store.register_definition(original)
    cleanup_registry.add("uf_register_fail")

    with pytest.raises(ValueError, match="版本未提升"):
        store.persist_definition(tmp_path, {**original, "label": "不应落盘"})

    assert store.load_all(tmp_path) == [original]
    assert get_factor("uf_register_fail").label == "旧定义"


def test_concurrent_same_version_updates_keep_disk_and_registry_consistent(
    tmp_path, monkeypatch, cleanup_registry,
) -> None:
    """同 ID 同版本并发更新中, 失败事务不得回滚另一成功事务的文件。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    original = {
        "id": "uf_concurrent",
        "kind": "custom",
        "version": 1,
        "label": "旧定义",
        "formula": "close",
        "status": "draft",
    }
    store.save_one(tmp_path, original)
    store.register_definition(original)
    cleanup_registry.add("uf_concurrent")

    first_entered_register = Event()
    second_finished_save = Event()
    real_save = store.save_one
    real_register = store.register_factor

    def _tracked_save(data_dir, definition):
        real_save(data_dir, definition)
        if definition["label"] == "并发 B":
            second_finished_save.set()

    def _delayed_register(spec):
        if spec.label == "并发 A":
            first_entered_register.set()
            second_finished_save.wait(timeout=0.2)
        real_register(spec)

    monkeypatch.setattr(store, "save_one", _tracked_save)
    monkeypatch.setattr(store, "register_factor", _delayed_register)
    update_a = {**original, "version": 2, "label": "并发 A"}
    update_b = {**original, "version": 2, "label": "并发 B"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.persist_definition, tmp_path, update_a)
        assert first_entered_register.wait(timeout=1)
        second = pool.submit(store.persist_definition, tmp_path, update_b)
        outcomes = []
        for future in (first, second):
            try:
                outcomes.append(("ok", future.result(timeout=2)))
            except ValueError as exc:
                outcomes.append(("error", str(exc)))

    assert [kind for kind, _ in outcomes].count("ok") == 1
    assert [kind for kind, _ in outcomes].count("error") == 1
    persisted = store.load_all(tmp_path)[0]
    registered = get_factor("uf_concurrent")
    assert persisted["version"] == registered.version == 2
    assert persisted["label"] == registered.label
