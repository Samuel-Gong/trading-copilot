"""因子 API (validate / trial) 契约测试 (P2)。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.factors import router


@pytest.fixture()
def cleanup_registry():
    """测试注册的自定义因子在用例后注销, 不污染全局注册表 (快照测试依赖 77 基线)。"""
    created: set[str] = set()
    yield created
    from app.factors.registry import unregister_factor

    for fid in created:
        unregister_factor(fid)


class _FakeEngine:
    """合成面板: 3 只股票日收益固定 1%/2%/3%, 任何按价格排序的因子 IC 恒为 1。"""

    def __init__(self, n_days: int = 40) -> None:
        rows = []
        end = date.today()
        for index in range(n_days):
            day = end - timedelta(days=n_days - 1 - index)
            for symbol_id, daily_return in (("A", 0.01), ("B", 0.02), ("C", 0.03)):
                # 正基数且增速同序: C 永远最高价且回报最高 → 按价格排序的因子 IC 恒为 1
                rows.append({
                    "symbol": symbol_id,
                    "date": day,
                    "close": (1.0 + daily_return) ** index * 10.0 * (ord(symbol_id) - ord("A") + 1),
                })
        self.panel = pl.DataFrame(rows).sort(["symbol", "date"])
        self.last_range: tuple[date, date] | None = None

    def load_panel(self, symbols, start, end, *, columns=None, asset_type="stock", **_kwargs):
        self.last_range = (start, end)
        frame = self.panel
        if columns is not None:
            for column in columns:
                if column not in frame.columns:
                    frame = frame.with_columns(pl.lit(None).cast(pl.Float64).alias(column))
            frame = frame.select(columns)
        return frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))


def _client(with_engine: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    if with_engine:
        app.state.backtest_engine = _FakeEngine()
    return TestClient(app)


def test_validate_ok_formula() -> None:
    response = _client().post("/api/factors/validate", json={"formula": "rank(-ts_sum(change_pct, 5))"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["errors"] == []
    assert payload["dependencies"] == ["change_pct"]
    assert payload["warmup_bars"] == 5
    assert payload["cross_sectional"] is True


def test_validate_future_function_rejected() -> None:
    response = _client().post("/api/factors/validate", json={"formula": "rank(ts_delta(close, -5))"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "E005"
    assert "position" in payload["errors"][0]


def test_trial_golden_ic() -> None:
    response = _client(with_engine=True).post(
        "/api/factors/trial", json={"formula": "close", "days": 30},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["n_dates"] == 30  # 40 日面板, 首日无前收 → 39 个可算截面, 取最近 30
    assert payload["ic_mean"] == pytest.approx(1.0, abs=1e-9)
    assert payload["ic_win_rate"] == pytest.approx(1.0, abs=1e-9)
    # 恒定 IC 序列 std=0, IR 无定义 → None (除零保护)
    assert payload["ic_std"] in (None, 0.0)
    assert payload["ir"] is None
    assert len(payload["ic_series"]) == 30


def test_trial_and_save_preflight_default_to_beijing_today(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.api import factors as factors_api

    target = date.today() - timedelta(days=1)
    engine = _FakeEngine()
    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = engine
    monkeypatch.setattr(factors_api, "cn_today", lambda: target)

    response = TestClient(app).post(
        "/api/factors/trial",
        json={"formula": "close", "days": 20},
    )
    assert response.status_code == 200
    assert engine.last_range is not None
    assert engine.last_range[1] == target

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        backtest_engine=engine,
    )))
    factors_api._trial_nonempty(request, "close")
    assert engine.last_range is not None
    assert engine.last_range[1] == target


def test_trial_compile_failure_400() -> None:
    response = _client(with_engine=True).post(
        "/api/factors/trial", json={"formula": "nope_col + 1", "days": 30},
    )
    assert response.status_code == 400
    body = response.json()["detail"]
    assert body["errors"][0]["code"] == "E001"


def test_trial_computes_virtual_factor_via_shared_path() -> None:
    # 引用虚拟因子 ma20_bias: 试算端点复用 _compute_missing_factors 补算路径
    # (compute_indicators 算 ma20 + materialize 物化 bias), 3 列粗面板即可出结果
    response = _client(with_engine=True).post(
        "/api/factors/trial", json={"formula": "ma20_bias", "days": 20},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["n_dates"] == 20
    assert payload["ic_mean"] is not None


def test_trial_attaches_recursive_financial_dependency(
    tmp_path,
    cleanup_registry,
) -> None:
    from pathlib import Path
    from types import SimpleNamespace

    from app.factors import store
    from app.factors.registry import register_factor

    factor_id = "uf_financial_trial"
    register_factor(store.to_spec({
        "id": factor_id,
        "kind": "custom",
        "version": 1,
        "label": "财务试算",
        "formula": "rank(roe_latest)",
        "status": "draft",
    }))
    cleanup_registry.add(factor_id)
    metrics = Path(tmp_path) / "financials" / "metrics" / "part.parquet"
    metrics.parent.mkdir(parents=True)
    announce = (date.today() - timedelta(days=60)).isoformat()
    pl.DataFrame({
        "symbol": ["A", "B", "C"],
        "period_end": ["2026-03-31"] * 3,
        "announce_date": [announce] * 3,
        "bps": [1.0, 2.0, 3.0],
        "roe": [10.0, 20.0, 30.0],
        "gross_margin": [1.0, 2.0, 3.0],
        "net_margin": [1.0, 2.0, 3.0],
        "revenue_yoy": [1.0, 2.0, 3.0],
        "net_income_yoy": [1.0, 2.0, 3.0],
        "debt_to_asset_ratio": [1.0, 2.0, 3.0],
    }).write_parquet(metrics)
    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))

    response = TestClient(app).post(
        "/api/factors/trial",
        json={"formula": factor_id, "days": 20},
    )

    assert response.status_code == 200
    assert response.json()["n_dates"] == 20


def test_trial_rejects_financial_custom_factor_for_etf(cleanup_registry) -> None:
    from app.factors import store
    from app.factors.registry import register_factor

    factor_id = "uf_stock_only_trial"
    register_factor(store.to_spec({
        "id": factor_id,
        "kind": "custom",
        "version": 1,
        "label": "股票专属试算",
        "formula": "roe_latest",
        "status": "draft",
    }))
    cleanup_registry.add(factor_id)

    response = _client(with_engine=True).post(
        "/api/factors/trial",
        json={"formula": factor_id, "asset_type": "etf", "days": 20},
    )

    assert response.status_code == 400
    assert "不支持资产类型 etf" in response.json()["detail"]


def test_group_and_status_update_after_registry_load(tmp_path, cleanup_registry) -> None:
    """改分组/状态在因子已注册 (启动加载后) 的真实路径下可用。

    回归保护: 同版本直接 register 会被注册表拒绝 ("版本未提升"),
    端点必须先注销再按新元数据注册。
    """
    from pathlib import Path
    from types import SimpleNamespace

    from app.factors.registry import get_factor

    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)

    created = client.post("/api/factors/custom", json={
        "id": "uf_group_test", "label": "分组测试", "formula": "rank(-ts_sum(change_pct, 5))",
    })
    assert created.status_code == 200
    factor_id = created.json()["id"]
    cleanup_registry.add(factor_id)

    from app.factors import store

    store.load_into_registry(Path(tmp_path))  # 模拟重启后的注册状态
    registered = get_factor(factor_id)
    assert registered is not None and registered.group == "自定义"

    renamed = client.post(f"/api/factors/custom/{factor_id}/group", json={"group": "我的动量组"})
    assert renamed.status_code == 200
    assert renamed.json()["group"] == "我的动量组"
    refreshed = get_factor(factor_id)
    assert refreshed is not None and refreshed.group == "我的动量组"  # 注册表同步
    on_disk = next(d for d in store.load_all(Path(tmp_path)) if d["id"] == factor_id)
    assert on_disk["group"] == "我的动量组"  # 磁盘持久化

    activated = client.post(f"/api/factors/custom/{factor_id}/status", json={"status": "active"})
    assert activated.status_code == 200  # 修复前: 400 "版本未提升"
    assert get_factor(factor_id).stability == "stable"

    bad = client.post(f"/api/factors/custom/{factor_id}/group", json={"group": "   "})
    assert bad.status_code == 400  # 空白分组名 fail-closed


def test_update_custom_factor_bumps_version(tmp_path, cleanup_registry) -> None:
    """编辑已有自定义因子: 版本提升注册 + 公式变化回 draft + 试算门禁。"""
    from pathlib import Path
    from types import SimpleNamespace

    from app.factors import store
    from app.factors.registry import get_factor

    cleanup_registry.add("uf_edit_test")
    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)
    data_dir = Path(tmp_path)

    created = client.post("/api/factors/custom", json={
        "id": "uf_edit_test", "label": "编辑测试", "formula": "rank(-ts_sum(change_pct, 5))",
    })
    assert created.status_code == 200
    assert created.json()["version"] == 1

    # 激活后再编辑: 公式变化 → 新版本 + 回 draft (生命周期语义)
    activated = client.post("/api/factors/custom/uf_edit_test/status", json={"status": "active"})
    assert activated.status_code == 200

    updated = client.post("/api/factors/custom/uf_edit_test/update", json={
        "label": "编辑测试v2", "group": "新分组", "formula": "rank(-ts_sum(change_pct, 10))",
        "description": "窗口从 5 改 10", "direction": "low",
    })
    assert updated.status_code == 200
    body = updated.json()
    assert body["version"] == 2 and body["status"] == "draft"

    spec = get_factor("uf_edit_test")
    assert spec is not None
    assert spec.version == 2 and spec.group == "新分组" and spec.label == "编辑测试v2"
    assert "change_pct" in spec.dependencies
    on_disk = next(d for d in store.load_all(data_dir) if d["id"] == "uf_edit_test")
    assert on_disk["version"] == 2 and on_disk["status"] == "draft"

    # 仅改元数据 (公式不变): 版本仍提升, 状态保留 (不回 draft)
    client.post("/api/factors/custom/uf_edit_test/status", json={"status": "active"})
    meta = client.post("/api/factors/custom/uf_edit_test/update", json={
        "label": "仅改名字", "group": "新分组", "formula": "rank(-ts_sum(change_pct, 10))",
    })
    assert meta.status_code == 200
    assert meta.json() == {"ok": True, "id": "uf_edit_test", "version": 3, "status": "active"}

    missing = client.post("/api/factors/custom/uf_nope/update", json={
        "label": "x", "formula": "close",
    })
    assert missing.status_code == 404


def test_child_update_refreshes_transitive_factor_metadata(
    tmp_path,
    monkeypatch,
    cleanup_registry,
) -> None:
    from pathlib import Path
    from types import SimpleNamespace

    from app.api import factors as factors_api
    from app.factors.registry import get_factor

    ids = {"uf_meta_child", "uf_meta_parent", "cf_meta_outer"}
    cleanup_registry.update(ids)
    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)

    assert client.post("/api/factors/custom", json={
        "id": "uf_meta_child",
        "label": "元数据子因子",
        "formula": "close",
    }).status_code == 200
    assert client.post("/api/factors/custom", json={
        "id": "uf_meta_parent",
        "label": "元数据父因子",
        "formula": "rank(uf_meta_child)",
    }).status_code == 200
    assert client.post("/api/factors/composite", json={
        "id": "cf_meta_outer",
        "label": "元数据外层组合",
        "members": {"uf_meta_parent": 1.0, "amount": 1.0},
    }).status_code == 200

    monkeypatch.setattr(factors_api, "_trial_nonempty", lambda *_args, **_kwargs: None)
    response = client.post("/api/factors/custom/uf_meta_child/update", json={
        "label": "财务子因子",
        "formula": "ts_mean(roe_latest, 20)",
    })

    assert response.status_code == 200
    for factor_id in ids:
        spec = get_factor(factor_id)
        assert spec is not None
        expected_dependencies = (
            {"roe_latest", "amount"}
            if factor_id == "cf_meta_outer"
            else {"roe_latest"}
        )
        assert spec.dependencies == frozenset(expected_dependencies)
        assert spec.warmup_bars == 20
        assert spec.asset_types == frozenset({"stock"})
        assert spec.pit is True
        assert spec.pit_source == "financial_announce"


def test_dependent_metadata_refresh_failure_rolls_back_atomically(
    tmp_path,
    monkeypatch,
    cleanup_registry,
) -> None:
    from pathlib import Path
    from types import SimpleNamespace

    from app.api import factors as factors_api
    from app.factors import store
    from app.factors.registry import get_factor

    child_id = "uf_meta_rollback_child"
    parent_id = "uf_meta_rollback_parent"
    cleanup_registry.update({child_id, parent_id})
    data_dir = Path(tmp_path)
    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    client = TestClient(app)
    assert client.post("/api/factors/custom", json={
        "id": child_id,
        "label": "回滚子因子",
        "formula": "close",
    }).status_code == 200
    assert client.post("/api/factors/custom", json={
        "id": parent_id,
        "label": "回滚父因子",
        "formula": child_id,
    }).status_code == 200
    child_path = data_dir / "user_data" / "custom_factors" / f"{child_id}.json"
    previous_bytes = child_path.read_bytes()
    previous_child = get_factor(child_id)
    previous_parent = get_factor(parent_id)
    real_to_spec = store.to_spec

    def fail_parent(definition):
        if definition.get("id") == parent_id and get_factor(child_id).pit:
            raise ValueError("synthetic dependent refresh failure")
        return real_to_spec(definition)

    monkeypatch.setattr(factors_api, "_trial_nonempty", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(store, "to_spec", fail_parent)
    response = client.post(f"/api/factors/custom/{child_id}/update", json={
        "label": "财务子因子",
        "formula": "roe_latest",
    })

    assert response.status_code == 400
    assert "synthetic dependent refresh failure" in response.json()["detail"]
    assert child_path.read_bytes() == previous_bytes
    assert get_factor(child_id) is previous_child
    assert get_factor(parent_id) is previous_parent


def test_trial_response_includes_newey_west_t() -> None:
    response = _client(with_engine=True).post(
        "/api/factors/trial", json={"formula": "close", "days": 30},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["n_dates"] == 30
    assert "t_newey_west" in payload  # 恒定 IC 序列下可为 None, 但字段必须存在


def test_delete_custom_and_composite_factor(tmp_path, cleanup_registry) -> None:
    """删除契约: 复合因子可直接删; 成员被复合引用时 409 列引用方, force 才放行。"""
    from pathlib import Path
    from types import SimpleNamespace

    from app.factors import store
    from app.factors.registry import get_factor

    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()  # 创建门禁需试算
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)
    data_dir = Path(tmp_path)

    client.post("/api/factors/custom", json={
        "id": "uf_del_member", "label": "被引用成员", "formula": "rank(-ts_sum(change_pct, 5))",
    })
    cleanup_registry.add("uf_del_member")
    created = client.post("/api/factors/composite", json={
        "id": "cf_del_test", "label": "删除测试组合", "members": {"uf_del_member": 1.0, "momentum_10d": -0.5},
    })
    assert created.status_code == 200
    cleanup_registry.add("cf_del_test")

    # 404: 不存在的因子
    assert client.delete("/api/factors/custom/cf_nope").status_code == 404

    # 409: 成员被复合因子引用 → fail-closed, 返回引用方列表
    blocked = client.delete("/api/factors/custom/uf_del_member")
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert "cf_del_test" in str(detail["references"])
    assert get_factor("uf_del_member") is not None  # 引用未解除, 因子仍在

    # 复合因子本身可直接删除 (无人引用它)
    removed = client.delete("/api/factors/custom/cf_del_test")
    assert removed.status_code == 200
    assert removed.json() == {"ok": True, "id": "cf_del_test", "removed_references": []}
    assert get_factor("cf_del_test") is None
    assert all(d["id"] != "cf_del_test" for d in store.load_all(data_dir))  # 磁盘已删

    # 引用解除后成员可正常删除
    freed = client.delete("/api/factors/custom/uf_del_member")
    assert freed.status_code == 200
    assert get_factor("uf_del_member") is None


def test_delete_with_strategy_reference_requires_force(tmp_path, cleanup_registry) -> None:
    """策略文件引用同样拦截: 409 且不产生删除副作用, force=true 放行。"""
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from app.factors.registry import get_factor

    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()  # 创建门禁需试算
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)
    data_dir = Path(tmp_path)

    client.post("/api/factors/custom", json={
        "id": "uf_strat_ref", "label": "策略引用", "formula": "rank(-ts_sum(change_pct, 5))",
    })
    cleanup_registry.add("uf_strat_ref")
    strategies_dir = data_dir / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "my_strategy.json").write_text(
        json.dumps({"name": "my_strategy", "factors": {"uf_strat_ref": 1.0}}), encoding="utf-8",
    )

    blocked = client.delete("/api/factors/custom/uf_strat_ref")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["references"] == ["strategies/my_strategy.json"]

    forced = client.delete("/api/factors/custom/uf_strat_ref?force=true")
    assert forced.status_code == 200
    assert forced.json()["removed_references"] == ["strategies/my_strategy.json"]
    assert get_factor("uf_strat_ref") is None


def test_delete_with_nested_python_strategy_reference_is_side_effect_free(
    tmp_path, cleanup_registry,
) -> None:
    """实际 custom/ai/composite Python 策略引用必须在删除前被发现。"""
    from pathlib import Path
    from types import SimpleNamespace

    from app.factors import store
    from app.factors.registry import get_factor

    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)
    data_dir = Path(tmp_path)

    response = client.post("/api/factors/custom", json={
        "id": "uf_python_ref",
        "label": "Python 策略引用",
        "formula": "rank(-ts_sum(change_pct, 5))",
    })
    assert response.status_code == 200
    cleanup_registry.add("uf_python_ref")

    strategy_dir = data_dir / "strategies" / "custom"
    strategy_dir.mkdir(parents=True)
    (strategy_dir / "my_strategy.py").write_text(
        'REQUIRED_FEATURES = {"uf_python_ref"}\n', encoding="utf-8",
    )

    blocked = client.delete("/api/factors/custom/uf_python_ref")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["references"] == ["strategies/custom/my_strategy.py"]
    assert get_factor("uf_python_ref") is not None
    assert store.exists(data_dir, "uf_python_ref")


def test_delete_checks_strategy_override_and_custom_signal_references(
    tmp_path, cleanup_registry,
) -> None:
    """override 与自定义信号引用也必须阻止无副作用删除。"""
    from pathlib import Path
    from types import SimpleNamespace

    from app.factors import store
    from app.strategy import config as strategy_config
    from app.strategy import custom_signals

    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)
    data_dir = Path(tmp_path)
    factor_id = "uf_definition_ref"
    cleanup_registry.add(factor_id)

    created = client.post("/api/factors/custom", json={
        "id": factor_id,
        "label": "定义图引用",
        "formula": "rank(-ts_sum(change_pct, 5))",
    })
    assert created.status_code == 200
    strategy_config.save_override(
        data_dir,
        "alpha",
        {"scoring": {factor_id: 1.0}},
    )
    custom_signals.save_one(data_dir, {
        "id": "factor_ref",
        "name": "因子引用",
        "kind": "entry",
        "enabled": True,
        "conditions": [{
            "left": factor_id,
            "op": ">",
            "right": "0",
            "leftDays": 0,
            "rightDays": 0,
        }],
    })

    blocked = client.delete(f"/api/factors/custom/{factor_id}")

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["references"] == [
        "user_data/strategy_overrides/alpha.json",
        "user_data/custom_signals/factor_ref.json",
    ]
    assert store.exists(data_dir, factor_id)


def test_factor_update_invalidates_only_transitive_strategy_dependents(
    tmp_path, cleanup_registry,
) -> None:
    """因子语义变化清理直接/间接/信号引用策略, 保留无关导出缓存。"""
    from pathlib import Path
    from types import SimpleNamespace

    from app.services import strategy_cache
    from app.strategy import config as strategy_config
    from app.strategy import custom_signals

    data_dir = Path(tmp_path)
    calls: list[str] = []

    def strategy(strategy_id, *, scoring=None):
        return SimpleNamespace(
            meta={"id": strategy_id, "scoring": scoring or {}},
            required_features=frozenset(),
            entry_signals=[],
            exit_signals=[],
            matrix_strategy=None,
        )

    definitions = [
        strategy("alpha", scoring={"uf_cache_dependent": 1.0}),
        strategy("signal_user"),
        strategy("blend"),
        strategy("unrelated", scoring={"momentum_10d": 1.0}),
    ]
    strategy_engine = SimpleNamespace(
        strategy_definitions=lambda: definitions,
        find_dependents=lambda strategy_id: ["blend"] if strategy_id == "alpha" else [],
        invalidate_realtime_matrices=lambda: calls.append("matrix"),
    )
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=data_dir),
        clear_cache=lambda: calls.append("repo"),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.strategy_engine = strategy_engine
    app.state.monitor_engine = SimpleNamespace(
        invalidate_strategy_state=lambda: calls.append("monitor"),
    )
    app.state.repo = repo
    client = TestClient(app)

    for factor_id, formula in (
        ("uf_cache_target", "close"),
        ("uf_cache_dependent", "uf_cache_target"),
    ):
        response = client.post("/api/factors/custom", json={
            "id": factor_id,
            "label": factor_id,
            "formula": formula,
        })
        assert response.status_code == 200
        cleanup_registry.add(factor_id)

    custom_signals.save_one(data_dir, {
        "id": "factor_signal",
        "name": "因子信号",
        "kind": "entry",
        "enabled": True,
        "conditions": [{
            "left": "uf_cache_target",
            "op": ">",
            "right": "0",
            "leftDays": 0,
            "rightDays": 0,
        }],
    })
    strategy_config.save_override(
        data_dir,
        "signal_user",
        {"entry_signals": ["csg_factor_signal"]},
    )
    result = {"rows": [], "total": 0, "as_of": "2026-09-07"}
    strategy_cache.write_cache(data_dir, "2026-09-07", {
        strategy_id: result
        for strategy_id in ("alpha", "signal_user", "blend", "unrelated")
    })
    calls.clear()

    updated = client.post("/api/factors/custom/uf_cache_target/update", json={
        "label": "目标因子 v2",
        "formula": "rank(close)",
    })

    assert updated.status_code == 200
    cached = strategy_cache.read_cache(data_dir)
    assert cached is not None
    assert cached["results"] == {"unrelated": result}
    assert calls == ["matrix", "monitor", "repo"]


def test_factor_update_invalidation_failure_restores_disk_and_registry(
    tmp_path,
    cleanup_registry,
    monkeypatch,
) -> None:
    from pathlib import Path
    from types import SimpleNamespace

    from app.factors import store
    from app.factors.registry import get_factor
    from app.services import strategy_cache

    factor_id = "uf_factor_rollback"
    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)
    created = client.post("/api/factors/custom", json={
        "id": factor_id,
        "label": "回滚因子",
        "formula": "close",
    })
    assert created.status_code == 200
    cleanup_registry.add(factor_id)
    previous = next(item for item in store.load_all(tmp_path) if item["id"] == factor_id)
    previous_spec = get_factor(factor_id)
    monkeypatch.setattr(
        strategy_cache,
        "clear_cache",
        lambda _data_dir: (_ for _ in ()).throw(OSError("synthetic invalidation failure")),
    )

    with pytest.raises(OSError, match="synthetic invalidation failure"):
        client.post(f"/api/factors/custom/{factor_id}/update", json={
            "label": "不应生效",
            "formula": "close * 2",
        })

    assert next(item for item in store.load_all(tmp_path) if item["id"] == factor_id) == previous
    restored_spec = get_factor(factor_id)
    assert restored_spec is not None and previous_spec is not None
    assert restored_spec.version == previous_spec.version
    assert restored_spec.formula_text == previous_spec.formula_text


def test_delete_cannot_be_undone_by_concurrent_update(
    tmp_path, monkeypatch, cleanup_registry,
) -> None:
    """更新读到旧定义后与删除交错时, 最终不得把已删除因子复活。"""
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from threading import Event
    from types import SimpleNamespace

    from app.factors import store
    from app.factors.registry import get_factor

    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)
    data_dir = Path(tmp_path)
    factor_id = "uf_delete_update_race"
    cleanup_registry.add(factor_id)
    created = client.post("/api/factors/custom", json={
        "id": factor_id,
        "label": "删除更新竞态",
        "formula": "rank(-ts_sum(change_pct, 5))",
    })
    assert created.status_code == 200

    update_reached_persist = Event()
    delete_finished = Event()
    real_persist = store.persist_definition

    def _delayed_update_persist(data_dir_arg, definition, **kwargs):
        if definition.get("label") == "并发更新":
            update_reached_persist.set()
            delete_finished.wait(timeout=0.2)
        return real_persist(data_dir_arg, definition, **kwargs)

    def _delete():
        response = client.delete(f"/api/factors/custom/{factor_id}")
        delete_finished.set()
        return response

    monkeypatch.setattr(store, "persist_definition", _delayed_update_persist)
    with ThreadPoolExecutor(max_workers=2) as pool:
        update = pool.submit(client.post, f"/api/factors/custom/{factor_id}/update", json={
            "label": "并发更新",
            "formula": "rank(-ts_sum(change_pct, 5))",
        })
        assert update_reached_persist.wait(timeout=1)
        delete = pool.submit(_delete)
        assert update.result(timeout=2).status_code == 200
        assert delete.result(timeout=2).status_code == 200

    assert not store.exists(data_dir, factor_id)
    assert get_factor(factor_id) is None


def test_delete_blocks_concurrent_composite_reference_creation(
    tmp_path, monkeypatch, cleanup_registry,
) -> None:
    """引用扫描后并发创建复合因子时, 不得留下指向已删除成员的定义。"""
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from threading import Event
    from types import SimpleNamespace

    from app.api import factors as factors_api
    from app.factors import store
    from app.factors.registry import get_factor

    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)
    data_dir = Path(tmp_path)
    member_id = "uf_reference_race"
    composite_id = "cf_reference_race"
    cleanup_registry.update({member_id, composite_id})
    created = client.post("/api/factors/custom", json={
        "id": member_id,
        "label": "引用竞态成员",
        "formula": "rank(-ts_sum(change_pct, 5))",
    })
    assert created.status_code == 200

    scan_completed = Event()
    create_finished = Event()
    real_find_references = factors_api._find_references

    def _delayed_reference_scan(data_dir_arg, factor_id):
        references = real_find_references(data_dir_arg, factor_id)
        scan_completed.set()
        create_finished.wait(timeout=0.2)
        return references

    def _create_composite():
        response = client.post("/api/factors/composite", json={
            "id": composite_id,
            "label": "并发引用",
            "members": {member_id: 1.0, "momentum_10d": 1.0},
        })
        create_finished.set()
        return response

    monkeypatch.setattr(factors_api, "_find_references", _delayed_reference_scan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        delete = pool.submit(client.delete, f"/api/factors/custom/{member_id}")
        assert scan_completed.wait(timeout=1)
        create = pool.submit(_create_composite)
        assert delete.result(timeout=2).status_code == 200
        assert create.result(timeout=2).status_code == 400

    assert get_factor(member_id) is None
    assert get_factor(composite_id) is None
    assert not store.exists(data_dir, member_id)
    assert not store.exists(data_dir, composite_id)
