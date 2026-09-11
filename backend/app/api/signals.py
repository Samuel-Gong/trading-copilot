"""自定义信号 API 路由 — HTTP 请求 → 调用 custom_signals 模块 → 返回响应。

只做胶水：校验 → 持久化 → 失效缓存。不含表达式编译逻辑。
"""
from __future__ import annotations

import copy
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.services.definition_transactions import definitions_transaction
from app.strategy import custom_signals

router = APIRouter(prefix="/api/custom-signals", tags=["custom-signals"])
logger = logging.getLogger(__name__)


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _invalidate(request: Request) -> None:
    """失效自定义信号表达式缓存, 并清掉含旧信号列的计算缓存。

    信号增删会改变注入列集合: 只清表达式缓存不够, repo 内存缓存 /
    strategy 磁盘缓存里算好的历史窗口仍不含新 csg_ 列 (或仍含已删列),
    需要一并清除, 否则创建信号后立即运行策略仍会报缺列。
    """
    from app.indicators.pipeline import invalidate_custom_signals
    from app.services import strategy_cache

    first_error: Exception | None = None
    try:
        invalidate_custom_signals()
    except Exception as exc:
        logger.exception("自定义信号表达式缓存失效失败")
        first_error = exc

    strategy_engine = getattr(request.app.state, "strategy_engine", None)
    invalidate_matrices = getattr(strategy_engine, "invalidate_realtime_matrices", None)
    if callable(invalidate_matrices):
        try:
            invalidate_matrices()
        except Exception as exc:
            logger.exception("实时策略矩阵失效失败")
            if first_error is None:
                first_error = exc

    monitor_engine = getattr(request.app.state, "monitor_engine", None)
    invalidate_monitor = getattr(monitor_engine, "invalidate_strategy_state", None)
    if callable(invalidate_monitor):
        try:
            invalidate_monitor()
        except Exception as exc:
            logger.exception("策略监控状态失效失败")
            if first_error is None:
                first_error = exc

    try:
        strategy_cache.clear_cache(_data_dir(request))
    except Exception as exc:
        logger.exception("策略结果缓存失效失败")
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


def _invalidate_signal_mutation(
    request: Request,
    signal_id: str,
    previous: dict | None,
) -> None:
    """提交信号跨层失效；失败时恢复定义。调用方已持有事务锁。"""
    try:
        _invalidate(request)
    except Exception:
        data_dir = _data_dir(request)
        if previous is None:
            custom_signals.delete_one(data_dir, signal_id)
        else:
            custom_signals.save_one(data_dir, previous)
        try:
            _invalidate(request)
        except Exception:
            logger.exception("自定义信号回滚后的运行态清理失败")
        raise


class ConditionModel(BaseModel):
    left: str        # 字段名（须在白名单）
    op: str          # > >= < <= == !=
    right: str       # "field:xxx" 或数字字符串
    leftDays: int = 0    # 左字段取几日前 (0=当日, 默认)
    rightDays: int = 0   # 右字段取几日前 (仅 right 为字段时有意义)


class SignalModel(BaseModel):
    id: str
    name: str
    kind: str        # entry | exit | both
    conditions: list[ConditionModel]
    enabled: bool = True


class AIGenerateRequest(BaseModel):
    description: str


# ── 字段选项 / 运算符 ───────────────────────────────────


@router.get("/options")
def get_options():
    """返回可选字段与运算符，供前端下拉框使用。"""
    # 字段带中文标签（取自 ENRICHED_COLUMNS，回退为字段名本身）
    from app.indicators.pipeline import ENRICHED_COLUMNS, ENRICHED_COLUMNS_BY_CATEGORY

    allowed = custom_signals.allowed_fields()
    fields = [
        {"key": f, "label": ENRICHED_COLUMNS.get(f, f)}
        for f in sorted(allowed)
    ]
    # 字段分组 (只包含白名单内的字段, 供前端 optoptgroup 渲染)
    _GROUP_LABELS = {
        "basic": "基础", "ma": "均线 MA", "ema": "指数均线 EMA",
        "macd": "MACD", "boll": "布林带 BOLL", "kdj": "KDJ",
        "atr": "ATR", "volume": "量价", "extremes": "极值",
        "momentum": "动量", "volatility": "波动率", "rsi": "RSI",
    }
    # 行情类字段不在 ENRICHED_COLUMNS_BY_CATEGORY 里, 单独归一组
    quote_fields = {"open", "high", "low", "close", "volume", "amount",
                    "turnover_rate", "consecutive_limit_ups", "consecutive_limit_downs"}
    groups = [{"key": "quote", "label": "行情",
               "fields": [{"key": f, "label": ENRICHED_COLUMNS.get(f, f)}
                          for f in sorted(allowed & quote_fields)]}]
    for cat, label in _GROUP_LABELS.items():
        cat_fields = [f for f in ENRICHED_COLUMNS_BY_CATEGORY.get(cat, []) if f in allowed]
        if cat_fields:
            groups.append({"key": cat, "label": label,
                           "fields": [{"key": f, "label": ENRICHED_COLUMNS.get(f, f)} for f in cat_fields]})

    # 注册表因子 (虚拟/自定义/复合): 历史路径由 compute_signals 复用评分物化
    # 管线补算; 已是物化列的基础因子 (rsi_14 等) 上面已分组, 此处跳过。
    from app.factors.registry import all_factors

    factor_groups: dict[str, list[dict[str, str]]] = {}
    base_allowed = custom_signals.ALLOWED_FIELDS
    for spec in all_factors():
        if spec.id in base_allowed or spec.id not in allowed:
            continue
        label = spec.label
        if spec.warmup_bars > 1:
            label = f"{label} · 预热{spec.warmup_bars}日"
        if list(spec.asset_types) == ["stock"]:
            label = f"{label} · 仅股票"
        factor_groups.setdefault(spec.group or "因子", []).append({"key": spec.id, "label": label})
    for group_label, group_fields in factor_groups.items():
        groups.append({"key": f"factor:{group_label}", "label": f"因子 · {group_label}", "fields": group_fields})
        fields.extend(group_fields)

    return {
        "fields": fields,
        "groups": groups,
        "maxDays": custom_signals.MAX_DAYS,
        "operators": [">", ">=", "<", "<=", "==", "!="],
        "kinds": [
            {"key": "entry", "label": "入场"},
            {"key": "exit", "label": "出场"},
            {"key": "both", "label": "出入通用"},
        ],
    }


# ── 列表 ───────────────────────────────────────────────


@router.get("")
def list_signals(request: Request):
    sigs = custom_signals.load_all(_data_dir(request))
    return {"signals": sigs}


# ── 新建 / 更新 ────────────────────────────────────────


@router.post("")
def save_signal(req: SignalModel, request: Request):
    sig = req.model_dump()
    data_dir = _data_dir(request)
    with definitions_transaction(data_dir):
        try:
            custom_signals.validate(sig)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        previous = next(
            (
                copy.deepcopy(item)
                for item in custom_signals.load_all(data_dir)
                if str(item.get("id")) == str(sig["id"])
            ),
            None,
        )
        custom_signals.save_one(data_dir, sig)
        _invalidate_signal_mutation(request, str(sig["id"]), previous)
    return {"ok": True, "signal": sig}


# ── AI 生成 ─────────────────────────────────────────────


@router.post("/ai/generate")
async def ai_generate_signal(req: AIGenerateRequest):
    """AI 根据自然语言描述生成自定义信号条件。

    不落盘：只返回 {name, conditions} 供前端回填表单，由用户确认后走
    常规 save 流程。校验复用 custom_signals.validate()（白名单安全闸门）。
    """
    from app.services.ai_provider import generate_ai_text
    from app.strategy import custom_signals_ai

    description = req.description.strip()
    if not description:
        raise HTTPException(status_code=400, detail="请先描述信号思路")
    if len(description) > 500:
        raise HTTPException(status_code=400, detail="描述过长（最多 500 字）")

    messages = custom_signals_ai.build_messages(description)
    try:
        # max_tokens=None 不传上限: 推理模型思考 token 计入预算, 显式限制
        # 会挤占正文导致 JSON 截断/0 字 (与四个分析器同因, 见 0ee3aa8)
        text = await generate_ai_text(messages, temperature=0.2, max_tokens=None)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"AI 生成失败: {e}") from e

    try:
        return custom_signals_ai.parse_and_validate(text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ── 删除 ───────────────────────────────────────────────


def _find_references(data_dir: Path, signal_id: str) -> list[str]:
    """扫描策略源、override 和监控规则中对自定义信号的引用。"""
    needle = custom_signals.column_name(signal_id)
    references: list[str] = []
    candidates: list[Path] = []
    strategies_dir = data_dir / "strategies"
    if strategies_dir.is_dir():
        candidates.extend(
            path for path in sorted(strategies_dir.rglob("*"))
            if path.is_file() and path.suffix in {".json", ".py"}
        )
    for relative_dir in (
        Path("user_data/strategy_overrides"),
        Path("user_data/monitor_rules"),
    ):
        directory = data_dir / relative_dir
        if directory.is_dir():
            candidates.extend(sorted(directory.glob("*.json")))
    for path in candidates:
        relative = path.relative_to(data_dir).as_posix()
        try:
            if needle in path.read_text(encoding="utf-8"):
                references.append(relative)
        except (OSError, UnicodeError):
            references.append(f"{relative} (无法验证)")
    return references


@router.delete("/{signal_id}")
def delete_signal(
    signal_id: str,
    request: Request,
    force: bool = Query(default=False),
):
    if not custom_signals.ID_RE.match(signal_id):
        raise HTTPException(status_code=400, detail="信号 id 非法")
    data_dir = _data_dir(request)
    with definitions_transaction(data_dir):
        if not (data_dir / "user_data" / "custom_signals" / f"{signal_id}.json").is_file():
            raise HTTPException(status_code=404, detail="信号不存在")
        references = _find_references(data_dir, signal_id)
        if references and not force:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "该信号仍有引用, 拒绝删除 (可带 force=true 强制)",
                    "references": references,
                },
            )
        previous = next(
            (
                copy.deepcopy(item)
                for item in custom_signals.load_all(data_dir)
                if str(item.get("id")) == signal_id
            ),
            None,
        )
        deleted = custom_signals.delete_one(data_dir, signal_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="信号不存在")
        _invalidate_signal_mutation(request, signal_id, previous)
    return {"ok": True, "removed_references": references}
