"""因子注册表 API — 因子库 (P1) + 公式校验/试算 (P2) + 自定义/复合因子 CRUD (P3)。"""
from __future__ import annotations

import copy
import logging
from datetime import timedelta

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.factors import store
from app.factors.dsl import FACTOR_COLUMN, compile_formula
from app.factors.registry import all_factors, unregister_factor
from app.market_time import cn_today

router = APIRouter(prefix="/api/factors", tags=["factors"])
logger = logging.getLogger(__name__)


@router.get("")
def list_factors(asset_type: str | None = Query(default=None, pattern="^(stock|etf)$")) -> dict:
    """注册表因子列表; asset_type 过滤适用资产 (财务因子仅股票)。"""
    specs = all_factors(asset_type=asset_type)
    return {
        "factors": [
            {
                "id": spec.id,
                "label": spec.label,
                "group": spec.group,
                "kind": spec.kind,
                "version": spec.version,
                "formula": spec.formula_text,
                "direction": spec.direction,
                "unit": spec.unit,
                "warmup_bars": spec.warmup_bars,
                "pit": spec.pit,
                "asset_types": sorted(spec.asset_types),
                "stability": spec.stability,
                "scale_free": spec.scale_free,
                "dependencies": sorted(spec.dependencies),
            }
            for spec in specs
        ]
    }


class FormulaValidateRequest(BaseModel):
    formula: str = Field(..., min_length=1, max_length=2000)


def _compiled_payload(compiled) -> dict:
    return {
        "ok": compiled.ok,
        "errors": [error.to_dict() for error in compiled.errors],
        "dependencies": sorted(compiled.dependencies),
        "referenced_factors": sorted(compiled.referenced_factors),
        "warmup_bars": compiled.warmup_bars,
        "cross_sectional": compiled.cross_sectional,
    }


@router.post("/validate")
def validate_formula(req: FormulaValidateRequest) -> dict:
    """公式校验: 语法/语义/窗口纪律/依赖推导, 编译期 fail-closed。"""
    return _compiled_payload(compile_formula(req.formula))


class FormulaTrialRequest(FormulaValidateRequest):
    asset_type: str = Field(default="stock", pattern="^(stock|etf)$")
    days: int = Field(default=40, ge=20, le=120)


@router.post("/trial")
def trial_formula(req: FormulaTrialRequest, request: Request) -> dict:
    """公式试算: 最近 N 个交易日截面 Rank IC 快照 (复用回测面板与虚拟因子物化路径)。"""
    compiled = compile_formula(req.formula)
    if not compiled.ok:
        raise HTTPException(status_code=400, detail={"errors": [error.to_dict() for error in compiled.errors]})

    from app.backtest.factor import validate_factor_asset_types

    try:
        validate_factor_asset_types(sorted(compiled.referenced_factors), req.asset_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from app.api.backtest import _get_engine

    # 交易日 → 自然日换算 (A股年均 243 交易日 ≈ 1.48 自然日/交易日), 留 buffer
    calendar_days = int((compiled.warmup_bars + req.days) * 1.6) + 15
    today = cn_today()
    start = today - timedelta(days=calendar_days)
    # 面板基础物理列 (load_panel 只返回 parquet 物理列, 因子列由补算路径生成)
    from app.backtest.factor import fundamental_dependencies

    fundamental_names = sorted(fundamental_dependencies(compiled.referenced_factors))
    base_columns = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
    if "pb_latest" in fundamental_names:
        base_columns.append("raw_close")
    if "consecutive_limit_ups" in compiled.dependencies:
        base_columns.append("consecutive_limit_ups")
    engine = _get_engine(request)
    panel = engine.load_panel(None, start, today, columns=base_columns, asset_type=req.asset_type)
    if panel.is_empty():
        raise HTTPException(status_code=400, detail="当前数据目录无可用历史数据, 无法试算")

    # 复用检验引擎同一条补算路径 (compute_indicators + 虚拟因子物化), 禁止第二套计算逻辑
    from app.backtest.factor import FactorBacktestService
    from app.backtest.fundamentals import (
        attach_fundamental_factors,
        load_fundamental_snapshot,
    )

    if fundamental_names:
        panel = attach_fundamental_factors(
            panel,
            load_fundamental_snapshot(_data_dir(request)),
            fundamental_names,
        )

    physical = set(panel.columns)
    to_compute = set(compiled.referenced_factors) | {
        dep for dep in compiled.dependencies if dep not in physical
    }
    if to_compute:
        panel = FactorBacktestService._compute_missing_factors(panel, to_compute)

    if compiled.frame_transform is None:
        raise HTTPException(status_code=500, detail="编译产物缺少帧变换")
    prepared = compiled.frame_transform(panel)
    if prepared is None:
        raise HTTPException(
            status_code=400,
            detail={"errors": [{
                "code": "E013", "message": "依赖列不可用: 面板缺少公式所需列",
                "position": {"offset": 0, "line": 1},
                "detail": {"missing": sorted((compiled.dependencies | compiled.referenced_factors) - set(panel.columns))},
            }]},
        )

    total_rows = panel.height
    frame = (
        prepared
        .with_columns(
            (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1.0).alias("_next_return")
        )
        .filter(pl.col(FACTOR_COLUMN).is_not_null())
        .unique(subset=["symbol", "date"], keep="last")
        .sort(["symbol", "date"])
    )
    non_null_rows = frame.height
    if non_null_rows == 0:
        return {
            "ok": True, "n_dates": 0, "null_ratio": 1.0,
            "ic_mean": None, "ic_std": None, "ir": None, "ic_win_rate": None,
            "ic_series": [], "message": "试算区间内公式输出全为空 (检查预热窗口与数据范围)",
        }

    ic_frame = (
        frame.filter(pl.col("_next_return").is_not_null())
        .group_by("date")
        .agg(
            pl.corr(pl.col(FACTOR_COLUMN).rank(method="average"), pl.col("_next_return").rank(method="average")).alias("ic"),
            pl.len().alias("n_symbols"),
        )
        .filter(pl.col("ic").is_not_null())
        .sort("date")
        .tail(req.days)
    )
    ic_series = [
        {"date": str(row["date"]), "ic": round(row["ic"], 4), "n_symbols": row["n_symbols"]}
        for row in ic_frame.to_dicts()
    ]
    if ic_frame.is_empty():
        return {
            "ok": True, "n_dates": 0, "null_ratio": round(1.0 - non_null_rows / max(total_rows, 1), 4),
            "ic_mean": None, "ic_std": None, "ir": None, "ic_win_rate": None,
            "ic_series": [], "message": "无有效 IC 截面 (需每期 ≥2 只标的)",
        }
    stats = ic_frame.select(
        pl.col("ic").mean().alias("mean"),
        pl.col("ic").std(ddof=0).alias("std"),
        (pl.col("ic") > 0).mean().alias("win"),
    ).row(0, named=True)
    ic_std = stats["std"]
    # Newey-West t (lag=1): 与检验页同源口径, 样本过少时不给 (fail-closed)
    t_newey_west = None
    if ic_frame.height >= 5:
        from app.backtest.stats_v2 import newey_west_t

        values = ic_frame["ic"].to_numpy()
        nw = newey_west_t(values, lag=1)
        if nw is not None:
            t_newey_west = round(float(nw[0]), 3)
    return {
        "ok": True,
        "n_dates": ic_frame.height,
        "null_ratio": round(1.0 - non_null_rows / max(total_rows, 1), 4),
        "ic_mean": round(stats["mean"], 4),
        "ic_std": None if ic_std is None else round(ic_std, 4),
        "ir": None if not ic_std or ic_std == 0 else round(stats["mean"] / ic_std, 3),
        "ic_win_rate": round(stats["win"], 4),
        "t_newey_west": t_newey_west,
        "ic_series": ic_series,
    }


# ── 自定义/复合因子 CRUD (P3) ──────────────────────────────


class CustomFactorCreateRequest(BaseModel):
    id: str | None = Field(default=None, max_length=48)
    label: str = Field(..., min_length=1, max_length=32)
    group: str = Field(default="自定义", max_length=16)
    formula: str = Field(..., min_length=1, max_length=2000)
    description: str = Field(default="", max_length=500)
    direction: str = Field(default="none", pattern="^(high|low|none)$")


class CompositeFactorCreateRequest(BaseModel):
    id: str | None = Field(default=None, max_length=48)
    label: str = Field(..., min_length=1, max_length=32)
    group: str = Field(default="组合", max_length=16)
    members: dict[str, float] = Field(..., min_length=2, max_length=8)
    description: str = Field(default="", max_length=500)
    direction: str = Field(default="none", pattern="^(high|low|none)$")


def _data_dir(request: Request):
    from pathlib import Path

    data_dir = getattr(getattr(request.app.state, "repo", None), "store", None)
    root = getattr(data_dir, "data_dir", None) if data_dir is not None else None
    if root is None:
        raise HTTPException(status_code=500, detail="数据目录不可用")
    return Path(root)


def _dependent_factor_ids(factor_id: str) -> set[str]:
    """返回因子本身及递归引用它的自定义/复合因子。"""
    affected = {factor_id}
    specs = all_factors()
    changed = True
    while changed:
        changed = False
        for spec in specs:
            if spec.id in affected:
                continue
            references = {member_id for member_id, _weight in spec.components}
            if spec.kind == "custom":
                compiled = compile_formula(spec.formula_text)
                if not compiled.ok:
                    raise ValueError(f"无法解析因子引用: {spec.id}")
                references.update(compiled.referenced_factors)
            if references & affected:
                affected.add(spec.id)
                changed = True
    return affected


def _factor_invalidation_plan(request: Request, factor_id: str) -> set[str] | None:
    """解析受因子语义变更影响的策略; 无法证明时返回 None 全量失效。"""
    engine = getattr(request.app.state, "strategy_engine", None)
    definitions = getattr(engine, "strategy_definitions", None)
    if engine is None or not callable(definitions):
        return None
    try:
        from app.strategy import config as strategy_config
        from app.strategy import custom_signals
        from app.strategy.scoring import effective_scoring

        data_dir = _data_dir(request)
        factor_ids = _dependent_factor_ids(factor_id)
        signal_columns: set[str] = set()
        for signal in custom_signals.load_all(data_dir):
            if signal.get("enabled") is False:
                continue
            for condition in signal.get("conditions", []):
                references = {str(condition.get("left") or "")}
                right = condition.get("right")
                if isinstance(right, str):
                    references.add(right.removeprefix("field:"))
                if references & factor_ids:
                    signal_columns.add(custom_signals.column_name(str(signal.get("id"))))
                    break

        affected: set[str] = set()
        for strategy in definitions():
            meta = getattr(strategy, "meta", {})
            strategy_id = str(meta.get("id") or "")
            if not strategy_id:
                raise ValueError("策略缺少 id")
            overrides = strategy_config.load_override(data_dir, strategy_id)
            scoring = effective_scoring(meta.get("scoring"), overrides)
            scoring_fields = {str(name) for name, weight in scoring.items() if weight}
            required = set(getattr(strategy, "required_features", ()) or ())
            required.update(meta.get("required_features", ()) or ())
            matrix = getattr(strategy, "matrix_strategy", None)
            matrix_fields = getattr(matrix, "required_fields", None)
            if callable(matrix_fields):
                required.update(str(name) for name in matrix_fields())
            entry_override = overrides.get("entry_signals")
            exit_override = overrides.get("exit_signals")
            entry_signals = (
                entry_override
                if isinstance(entry_override, list)
                else getattr(strategy, "entry_signals", ()) or ()
            )
            exit_signals = (
                exit_override
                if isinstance(exit_override, list)
                else getattr(strategy, "exit_signals", ()) or ()
            )
            signal_fields = {
                str(name)
                for name in (
                    list(entry_signals if isinstance(entry_signals, list) else ())
                    + list(exit_signals if isinstance(exit_signals, list) else ())
                )
            }
            if (
                scoring_fields & factor_ids
                or required & factor_ids
                or (required | signal_fields | scoring_fields) & signal_columns
            ):
                affected.add(strategy_id)

        pending = list(affected)
        while pending:
            current = pending.pop()
            for dependent in engine.find_dependents(current):
                dependent = str(dependent)
                if dependent not in affected:
                    affected.add(dependent)
                    pending.append(dependent)
        return affected
    except Exception:
        logger.exception("解析因子 %s 的策略依赖失败, 改为全量失效", factor_id)
        return None


def _invalidate_factor_runtime(
    request: Request,
    strategy_ids: set[str] | None,
) -> None:
    """失效因子变更涉及的信号、策略、监控与 enriched 缓存。"""
    from app.api.strategy import _invalidate_strategy_runtime
    from app.indicators.pipeline import invalidate_custom_signals

    first_error: Exception | None = None
    try:
        invalidate_custom_signals()
    except Exception as exc:
        logger.exception("自定义信号管线失效失败")
        first_error = exc
    engine = getattr(request.app.state, "strategy_engine", None)
    invalidate_matrices = getattr(engine, "invalidate_realtime_matrices", None)
    if callable(invalidate_matrices):
        try:
            invalidate_matrices()
        except Exception as exc:
            logger.exception("实时策略矩阵失效失败")
            if first_error is None:
                first_error = exc
    try:
        _invalidate_strategy_runtime(request, strategy_ids)
    except Exception as exc:
        logger.exception("因子关联策略运行态失效失败")
        if first_error is None:
            first_error = exc
    repo = request.app.state.repo
    clear_repo_cache = getattr(repo, "clear_cache", None)
    if callable(clear_repo_cache):
        try:
            clear_repo_cache()
        except Exception as exc:
            logger.exception("行情仓库缓存失效失败")
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def _restore_factor_state(
    data_dir,
    factor_id: str,
    previous_definition: dict | None,
    previous_spec,
    previous_registry=None,
) -> None:
    """恢复因子磁盘与注册表状态。调用方已持有定义事务锁。"""
    from app.factors.registry import register_factor, replace_dynamic_factors

    if previous_definition is None:
        store.delete_one(data_dir, factor_id)
    else:
        store.save_one(data_dir, previous_definition)
    if previous_registry is not None:
        replace_dynamic_factors(previous_registry)
        return
    unregister_factor(factor_id)
    if previous_spec is not None:
        register_factor(previous_spec)


def _invalidate_factor_mutation(
    request: Request,
    factor_id: str,
    affected: set[str] | None,
    previous_definition: dict | None,
    previous_spec,
    previous_registry=None,
) -> None:
    """提交跨层失效；失败时恢复定义与注册态。"""
    try:
        _invalidate_factor_runtime(request, affected)
    except Exception as original:
        try:
            _restore_factor_state(
                _data_dir(request),
                factor_id,
                previous_definition,
                previous_spec,
                previous_registry,
            )
        except Exception as recovery:
            raise RuntimeError(
                f"因子运行态失效且定义回滚失败: {recovery}"
            ) from original
        try:
            _invalidate_factor_runtime(request, affected)
        except Exception:
            logger.exception("因子定义回滚后的运行态清理失败")
        raise


def _slugify_id(label: str, prefix: str) -> str:
    base = "".join(ch if ch.isascii() and (ch.isalnum() or ch == "_") else "_" for ch in label.lower())
    candidate = f"{prefix}_{base}".strip("_")[:44]
    import re

    candidate = re.sub(r"_+", "_", candidate)
    return candidate or f"{prefix}_f"


def _resolve_id(requested: str | None, label: str, prefix: str) -> str:
    return requested.strip() if requested and requested.strip() else _slugify_id(label, prefix)


def _next_version(data_dir, factor_id: str) -> int:
    for definition in store.load_all(data_dir):
        if str(definition.get("id")) == factor_id:
            return int(definition.get("version", 1)) + 1
    return 1


def _trial_nonempty(request: Request, formula: str, asset_type: str = "stock") -> None:
    """保存前置校验: 公式在最近 40 个交易日有非空输出 (设计 §3.5, fail-closed)。"""
    compiled = compile_formula(formula)
    if not compiled.ok:
        raise HTTPException(status_code=400, detail={"errors": [e.to_dict() for e in compiled.errors]})
    from app.api.backtest import _get_engine
    from app.backtest.factor import FactorBacktestService, fundamental_dependencies
    from app.backtest.fundamentals import (
        attach_fundamental_factors,
        load_fundamental_snapshot,
    )

    calendar_days = int((compiled.warmup_bars + 40) * 1.6) + 15
    fundamental_names = sorted(fundamental_dependencies(compiled.referenced_factors))
    base_columns = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
    if "pb_latest" in fundamental_names:
        base_columns.append("raw_close")
    engine = _get_engine(request)
    today = cn_today()
    panel = engine.load_panel(
        None,
        today - timedelta(days=calendar_days),
        today,
        columns=base_columns,
        asset_type=asset_type,
    )
    if panel.is_empty():
        raise HTTPException(status_code=400, detail="当前无历史数据, 无法完成保存前试算 (fail-closed)")
    if fundamental_names:
        panel = attach_fundamental_factors(
            panel,
            load_fundamental_snapshot(_data_dir(request)),
            fundamental_names,
        )
    physical = set(panel.columns)
    to_compute = set(compiled.referenced_factors) | {d for d in compiled.dependencies if d not in physical}
    if to_compute:
        panel = FactorBacktestService._compute_missing_factors(panel, to_compute)
    prepared = compiled.frame_transform(panel) if compiled.frame_transform else None
    if prepared is None or prepared[FACTOR_COLUMN].is_not_null().sum() == 0:
        raise HTTPException(status_code=400, detail="公式在最近 40 个交易日输出全为空, 拒绝保存")


@router.post("/custom")
def create_custom_factor(req: CustomFactorCreateRequest, request: Request) -> dict:
    """保存自定义公式因子: 编译通过 + 服务端试算非空 (fail-closed)。"""
    data_dir = _data_dir(request)
    factor_id = _resolve_id(req.id, req.label, "uf")
    definition = {
        "id": factor_id,
        "kind": "custom",
        "version": 1,
        "label": req.label,
        "group": req.group,
        "formula": req.formula,
        "description": req.description,
        "direction": req.direction,
        "status": "draft",
        "created_at": store._now(),
        "updated_at": store._now(),
    }
    try:
        store.to_spec(definition)  # 先做 schema/id/编译校验
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _trial_nonempty(request, req.formula)
    try:
        with store.definitions_transaction(data_dir):
            from app.factors.registry import dynamic_factor_specs, get_factor

            previous_definition = next(
                (
                    copy.deepcopy(item)
                    for item in store.load_all(data_dir)
                    if str(item.get("id")) == factor_id
                ),
                None,
            )
            previous_spec = get_factor(factor_id)
            previous_registry = dynamic_factor_specs()
            definition["version"] = _next_version(data_dir, factor_id)
            store.persist_definition(data_dir, definition)
            affected = _factor_invalidation_plan(request, factor_id)
            _invalidate_factor_mutation(
                request,
                factor_id,
                affected,
                previous_definition,
                previous_spec,
                previous_registry,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "id": factor_id, "version": definition["version"]}


@router.post("/composite")
def create_composite_factor(req: CompositeFactorCreateRequest, request: Request) -> dict:
    """保存复合因子: 成员校验 + 循环引用检查 (无需试算, 值由成员物化路径计算)。"""
    data_dir = _data_dir(request)
    factor_id = _resolve_id(req.id, req.label, "cf")
    try:
        with store.definitions_transaction(data_dir):
            from app.factors.registry import dynamic_factor_specs, get_factor

            previous_definition = next(
                (
                    copy.deepcopy(item)
                    for item in store.load_all(data_dir)
                    if str(item.get("id")) == factor_id
                ),
                None,
            )
            previous_spec = get_factor(factor_id)
            previous_registry = dynamic_factor_specs()
            definition = {
                "id": factor_id,
                "kind": "composite",
                "version": _next_version(data_dir, factor_id),
                "label": req.label,
                "group": req.group,
                "members": req.members,
                "description": req.description,
                "direction": req.direction,
                "status": "draft",
                "created_at": store._now(),
                "updated_at": store._now(),
            }
            store.persist_definition(data_dir, definition)
            affected = _factor_invalidation_plan(request, factor_id)
            _invalidate_factor_mutation(
                request,
                factor_id,
                affected,
                previous_definition,
                previous_spec,
                previous_registry,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "id": factor_id, "version": definition["version"]}


class CustomFactorUpdateRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=32)
    group: str = Field(default="自定义", max_length=16)
    formula: str = Field(..., min_length=1, max_length=2000)
    description: str = Field(default="", max_length=500)
    direction: str = Field(default="none", pattern="^(high|low|none)$")


@router.post("/custom/{factor_id}/update")
def update_custom_factor(factor_id: str, req: CustomFactorUpdateRequest, request: Request) -> dict:
    """编辑已有自定义因子: 编译校验 + 试算非空 (与创建同一门禁) → 版本提升注册。

    公式变化时状态回 draft (生命周期语义: 编辑后需重新检验激活); 仅改名称/分组保留状态。
    """
    data_dir = _data_dir(request)
    try:
        with store.definitions_transaction(data_dir):
            snapshot = next(
                (item for item in store.load_all(data_dir) if str(item.get("id")) == factor_id),
                None,
            )
            if snapshot is None:
                raise HTTPException(status_code=404, detail=f"自定义因子不存在: {factor_id}")
            if str(snapshot.get("kind", "custom")) != "custom":
                raise HTTPException(
                    status_code=400,
                    detail=f"仅自定义因子支持公式编辑 (kind={snapshot.get('kind')})",
                )
            formula_changed = str(snapshot.get("formula")) != req.formula

        if formula_changed:
            _trial_nonempty(request, req.formula)

        with store.definitions_transaction(data_dir):
            from app.factors.registry import dynamic_factor_specs, get_factor

            target = next(
                (item for item in store.load_all(data_dir) if str(item.get("id")) == factor_id),
                None,
            )
            if target != snapshot:
                raise HTTPException(status_code=409, detail="因子已被其他请求修改, 请刷新后重试")
            affected = _factor_invalidation_plan(request, factor_id)
            previous_definition = copy.deepcopy(target)
            previous_spec = get_factor(factor_id)
            previous_registry = dynamic_factor_specs()
            target.update({
                "label": req.label,
                "group": req.group,
                "formula": req.formula,
                "description": req.description,
                "direction": req.direction,
                "version": int(target.get("version", 1)) + 1,
                "status": "draft" if formula_changed else str(target.get("status", "draft")),
                "updated_at": store._now(),
            })
            store.persist_definition(data_dir, target)
            _invalidate_factor_mutation(
                request,
                factor_id,
                affected,
                previous_definition,
                previous_spec,
                previous_registry,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "id": factor_id, "version": target["version"], "status": target["status"]}


def _find_references(data_dir, factor_id: str) -> list[str]:
    """扫描策略与复合因子定义中的引用 (删除前 fail-closed 检查)。"""
    references: list[str] = []
    strategies_dir = data_dir / "strategies"
    if strategies_dir.is_dir():
        for file in sorted(strategies_dir.rglob("*")):
            if not file.is_file() or file.suffix not in {".json", ".py"}:
                continue
            relative = file.relative_to(data_dir).as_posix()
            try:
                text = file.read_text(encoding="utf-8")
                if factor_id in text:
                    references.append(relative)
            except (OSError, UnicodeError):
                # 无法验证也视为引用风险; 非 force 删除必须失败闭合。
                references.append(f"{relative} (无法验证)")
    factors_dir = data_dir / "user_data" / "custom_factors"
    if factors_dir.is_dir():
        for file in sorted(factors_dir.glob("*.json")):
            if file.stem == factor_id:
                continue
            relative = f"custom_factors/{file.name}"
            try:
                if factor_id in file.read_text(encoding="utf-8"):
                    references.append(relative)
            except (OSError, UnicodeError):
                references.append(f"{relative} (无法验证)")
    for relative_dir in (
        "user_data/strategy_overrides",
        "user_data/custom_signals",
    ):
        directory = data_dir / relative_dir
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.json")):
            relative = file.relative_to(data_dir).as_posix()
            try:
                if factor_id in file.read_text(encoding="utf-8"):
                    references.append(relative)
            except (OSError, UnicodeError):
                references.append(f"{relative} (无法验证)")
    return references


@router.delete("/custom/{factor_id}")
def delete_custom_factor(factor_id: str, request: Request, force: bool = Query(default=False)) -> dict:
    """删除自定义/复合因子; 有引用时列出引用方并拒绝 (需 force)。"""
    data_dir = _data_dir(request)
    from app.factors.registry import dynamic_factor_specs, get_factor, registry_transaction

    with store.definitions_transaction(data_dir), registry_transaction():
        if get_factor(factor_id) is None and not store.exists(data_dir, factor_id):
            raise HTTPException(status_code=404, detail=f"因子不存在: {factor_id}")
        references = _find_references(data_dir, factor_id)
        if references and not force:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "该因子仍有引用, 拒绝删除 (可带 force=true 强制)",
                    "references": references,
                },
            )
        definition = next(
            (item for item in store.load_all(data_dir) if str(item.get("id")) == factor_id),
            None,
        )
        previous_spec = get_factor(factor_id)
        previous_registry = dynamic_factor_specs()
        affected = _factor_invalidation_plan(request, factor_id)
        deleted = store.delete_one(data_dir, factor_id)
        try:
            unregister_factor(factor_id)
        except ValueError as exc:
            if deleted and definition is not None:
                store.save_one(data_dir, definition)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _invalidate_factor_mutation(
            request,
            factor_id,
            affected,
            copy.deepcopy(definition),
            previous_spec,
            previous_registry,
        )
    return {"ok": True, "id": factor_id, "removed_references": references}


class FactorStatusRequest(BaseModel):
    status: str = Field(..., pattern="^(draft|active|watch|retired)$")


@router.post("/custom/{factor_id}/status")
def update_factor_status(factor_id: str, req: FactorStatusRequest, request: Request) -> dict:
    """生命周期状态迁移 (P4): draft->active->watch->retired, 编辑后回 draft。"""
    data_dir = _data_dir(request)
    try:
        with store.definitions_transaction(data_dir):
            from app.factors.registry import dynamic_factor_specs, get_factor

            target = next(
                (item for item in store.load_all(data_dir) if str(item.get("id")) == factor_id),
                None,
            )
            if target is None:
                raise HTTPException(status_code=404, detail=f"自定义因子不存在: {factor_id}")
            affected = _factor_invalidation_plan(request, factor_id)
            previous_definition = copy.deepcopy(target)
            previous_spec = get_factor(factor_id)
            previous_registry = dynamic_factor_specs()
            target["status"] = req.status
            target["updated_at"] = store._now()
            # 动态因子先注销再注册: 元数据变更 (status/group) 不提升版本,
            # 直接 register 会因"版本未提升"被拒 (启动加载后的真实路径)
            store.persist_definition(data_dir, target, replace_registered=True)
            _invalidate_factor_mutation(
                request,
                factor_id,
                affected,
                previous_definition,
                previous_spec,
                previous_registry,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "id": factor_id, "status": req.status}


class FactorGroupRequest(BaseModel):
    group: str = Field(..., min_length=1, max_length=24)


@router.post("/custom/{factor_id}/group")
def update_factor_group(factor_id: str, req: FactorGroupRequest, request: Request) -> dict:
    """修改单个自定义/复合因子的分组 (内置因子分组与快照/预设绑定, 不可改)。"""
    data_dir = _data_dir(request)
    group = req.group.strip()
    if not group:
        raise HTTPException(status_code=400, detail="分组名不能为空")
    try:
        with store.definitions_transaction(data_dir):
            target = next(
                (item for item in store.load_all(data_dir) if str(item.get("id")) == factor_id),
                None,
            )
            if target is None:
                raise HTTPException(status_code=404, detail=f"自定义因子不存在: {factor_id}")
            target["group"] = group
            target["updated_at"] = store._now()
            store.persist_definition(data_dir, target, replace_registered=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "id": factor_id, "group": group}
