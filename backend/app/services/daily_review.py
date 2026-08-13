"""按业务日期持久化并执行每日复盘档案。"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings
from app.services import (
    daily_analysis_graph,
    market_recap_reports,
    portfolio,
    review_news,
    stock_reports,
)
from app.services.market_recap import recap_market_once
from app.services.screener import ScreenerService
from app.strategy import config as strategy_config

_LOCK = threading.RLock()
_ACTIVE_TASKS: dict[str, asyncio.Task] = {}
_TIMEZONE = ZoneInfo("Asia/Shanghai")
_MAX_ROUTINES = 120
_MAX_CANDIDATES = 8
_SELECTION_METHOD = "strategy_pool_order_then_result_order_v1"
_MARKET_BLOCKED_MESSAGE = "前置步骤“市场环境”未完成"
_CANDIDATE_BLOCKED_MESSAGE = "前置步骤“策略候选”未完成"


def _path() -> Path:
    path = settings.data_dir / "user_data" / "daily_reviews.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_all() -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _write_all(items: list[dict]) -> None:
    items.sort(
        key=lambda item: (
            item.get("created_at", ""),
            int(item.get("run_number", 0) or 0),
            item.get("business_date", ""),
            item.get("id", ""),
        ),
        reverse=True,
    )
    path = _path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(items[:_MAX_ROUTINES], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(_TIMEZONE).isoformat(timespec="microseconds")


def _find(items: list[dict], routine_ref: str) -> dict | None:
    """按运行实例 ID 查找;ISO 日期仅作为读取最新一次复盘的兼容入口。"""
    by_id = next((item for item in items if item.get("id") == routine_ref), None)
    if by_id is not None:
        return by_id
    return next(
        (item for item in items if item.get("business_date") == routine_ref),
        None,
    )


def _upsert(routine: dict) -> None:
    with _LOCK:
        items = _read_all()
        items = [item for item in items if item.get("id") != routine["id"]]
        routine["updated_at"] = _now_iso()
        items.append(routine)
        _write_all(items)


def _get_raw(routine_ref: str) -> dict | None:
    with _LOCK:
        item = _find(_read_all(), routine_ref)
        return copy.deepcopy(item) if item else None


def _business_date(routine_ref: str) -> str:
    routine = _get_raw(routine_ref)
    return str(routine.get("business_date") if routine else routine_ref)


def _hydrate(routine: dict | None) -> dict | None:
    if routine is None:
        return None
    output = copy.deepcopy(routine)
    output.setdefault("run_number", 1)
    output.setdefault("cancel_requested_at", None)
    market_by_id = {item.get("id"): item for item in market_recap_reports.list_reports()}
    stock_by_id = {item.get("id"): item for item in stock_reports.list_reports()}
    market_snapshot = output["market_review"].pop("_report_snapshot", None)
    output["market_review"]["report"] = (
        market_by_id.get(output["market_review"].get("report_id")) or market_snapshot
    )
    screening = output.setdefault(
        "strategy_screening",
        {
            "status": "skipped",
            "selection_source": "legacy",
            "strategy_ids": [],
            "strategies": [],
            "candidate_count": 0,
            "selection_method": _SELECTION_METHOD,
            "error": None,
        },
    )
    strategy_ids = list(screening.get("strategy_ids") or [])
    screening.setdefault(
        "selection_source",
        "all_available" if strategy_ids else "legacy",
    )
    screening.setdefault(
        "strategies",
        [{"id": strategy_id, "name": strategy_id} for strategy_id in strategy_ids],
    )
    # 旧档案曾使用跨策略命中数和排名倒数和进行“共识排序”。当前候选只按
    # 策略池顺序及各策略原始结果顺序去重。hydrate 时统一暴露新口径。
    screening.pop("ranking_method", None)
    screening["selection_method"] = _SELECTION_METHOD
    output.setdefault(
        "news_context",
        {
            "status": "skipped",
            "source_status": "skipped",
            "items": [],
            "item_count": 0,
            "errors": [],
        },
    )
    scope_summary = output.setdefault("scope_summary", {})
    for field in (
        "position_count",
        "priced_position_count",
        "missing_price_count",
        "stale_price_count",
        "total_cost",
        "valuation_cost",
        "market_value",
        "unrealized_pnl",
        "trade_count",
        "realized_pnl",
        "total_fee",
        "total_tax",
    ):
        scope_summary.setdefault(field, 0)
    scope_summary.setdefault("unrealized_return_ratio", None)
    output.setdefault("candidates", [])
    for item in output.get("candidates", []):
        # 不改写历史原始文件。只在读取旧档案时隐藏已经废弃的候选共识分。
        item.pop("score", None)
        report_snapshot = item.pop("_report_snapshot", None)
        item["report"] = stock_by_id.get(item.get("report_id")) or report_snapshot
        item["graph"] = daily_analysis_graph.hydrate_graph(item.get("graph"))
        for node in item["graph"].get("nodes", []):
            node_input = node.get("input") or {}
            fields = node_input.get("fields")
            if isinstance(fields, list):
                node_input["fields"] = [
                    field
                    for field in fields
                    if field.get("key") != "consensus_score"
                ]
    for item in output.get("positions", []):
        report_snapshot = item.pop("_report_snapshot", None)
        item["report"] = stock_by_id.get(item.get("report_id")) or report_snapshot
        item["graph"] = daily_analysis_graph.hydrate_graph(item.get("graph"))
    return output


def get_routine(routine_ref: date | str) -> dict | None:
    key = routine_ref.isoformat() if isinstance(routine_ref, date) else routine_ref
    return _hydrate(_get_raw(key))


def list_routines() -> list[dict]:
    """返回轻量运行历史,不把报告正文和 Graph 全量塞进导航请求。"""
    with _LOCK:
        items = _read_all()
    chronological_by_date: dict[str, list[str]] = {}
    for item in sorted(
        items,
        key=lambda value: (value.get("created_at", ""), value.get("id", "")),
    ):
        chronological_by_date.setdefault(str(item.get("business_date", "")), []).append(
            str(item.get("id", ""))
        )

    summaries: list[dict] = []
    for item in items:
        targets = [*item.get("positions", []), *item.get("candidates", [])]
        ids_for_date = chronological_by_date.get(str(item.get("business_date", "")), [])
        fallback_number = (
            ids_for_date.index(str(item.get("id", ""))) + 1
            if str(item.get("id", "")) in ids_for_date
            else 1
        )
        summaries.append(
            {
                "id": item.get("id"),
                "business_date": item.get("business_date"),
                "run_number": item.get("run_number", fallback_number),
                "status": item.get("status", "interrupted"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "candidate_count": len(item.get("candidates", [])),
                "position_count": len(item.get("positions", [])),
                "completed_target_count": sum(
                    target.get("status") == "completed" for target in targets
                ),
                "target_count": len(targets),
            }
        )
    return summaries


def _normalize_strategy_ids(strategy_ids: list[str] | None) -> list[str] | None:
    if strategy_ids is None:
        return None
    return list(dict.fromkeys(value.strip() for value in strategy_ids if value.strip()))


def prepare_routine(
    repo,
    business_date: date,
    *,
    strategy_ids: list[str] | None = None,
) -> tuple[dict, bool]:
    key = business_date.isoformat()
    requested_strategy_ids = _normalize_strategy_ids(strategy_ids)
    selection_source = (
        "all_available" if requested_strategy_ids is None else "screener_pool"
    )
    snapshot = portfolio.get_snapshot(repo, business_date, exact_price_date=True)
    frozen_positions: list[dict] = []
    for account in snapshot["accounts"]:
        for position in account["positions"]:
            frozen_positions.append(
                {
                    "account_id": account["id"],
                    "account_name": account["name"],
                    "source_ref": f"{account['id']}:{position['symbol']}",
                    "symbol": position["symbol"],
                    "name": position["name"],
                    "asset_type": position["asset_type"],
                    "quantity": position["quantity"],
                    "average_cost": position["average_cost"],
                    "purchase_date": position.get("purchase_date"),
                    "note": position["note"],
                    "total_cost": position["total_cost"],
                    "current_price": position["current_price"],
                    "price_date": position["price_date"],
                    "price_available": position["price_available"],
                    "price_stale": position["price_stale"],
                    "market_value": position["market_value"],
                    "unrealized_pnl": position["unrealized_pnl"],
                    "unrealized_return_ratio": position["unrealized_return_ratio"],
                    "news": [],
                    "status": "pending",
                    "graph": daily_analysis_graph.new_graph(
                        target_type="position",
                        target_ref=f"{account['id']}:{position['symbol']}",
                        symbol=position["symbol"],
                    ),
                    "report_id": None,
                    "_report_snapshot": None,
                    "error": None,
                }
            )
    now = _now_iso()
    routine_id = f"daily_review_{uuid.uuid4().hex}"
    routine = {
        "id": routine_id,
        "business_date": key,
        "run_number": 1,
        "status": "running",
        "cancel_requested_at": None,
        "market_review": {
            "status": "pending",
            "report_id": None,
            "_report_snapshot": None,
            "error": None,
        },
        "news_context": {
            "status": "pending",
            "source_status": "pending",
            "as_of": key,
            "items": [],
            "item_count": 0,
            "errors": [],
        },
        "strategy_screening": {
            "status": "pending",
            "selection_source": selection_source,
            "strategy_ids": requested_strategy_ids or [],
            "strategies": [],
            "candidate_count": 0,
            "selection_method": _SELECTION_METHOD,
            "error": None,
        },
        "scope_summary": {
            field: snapshot[field]
            for field in (
                "position_count",
                "priced_position_count",
                "missing_price_count",
                "stale_price_count",
                "total_cost",
                "valuation_cost",
                "market_value",
                "unrealized_pnl",
                "unrealized_return_ratio",
                "trade_count",
                "realized_pnl",
                "total_fee",
                "total_tax",
            )
        },
        "positions": frozen_positions,
        "candidates": [],
        "created_at": now,
        "updated_at": now,
    }

    with _LOCK:
        items = _read_all()
        same_date_items = [item for item in items if item.get("business_date") == key]
        routine["run_number"] = max(
            [
                len(same_date_items),
                *(int(item.get("run_number", 0) or 0) for item in same_date_items),
            ]
        ) + 1
        items.append(routine)
        _write_all(items)
    return _hydrate(copy.deepcopy(routine)), True


def prepare_retry(routine_ref: date | str) -> tuple[dict | None, bool]:
    """把失败或中断子项恢复为待执行,保留冻结范围与已完成报告。"""
    key = routine_ref.isoformat() if isinstance(routine_ref, date) else routine_ref
    with _LOCK:
        items = _read_all()
        routine = _find(items, key)
        if not routine:
            return None, False

        changed = False
        if routine["market_review"]["status"] in {"failed", "interrupted"}:
            routine["market_review"].update(
                status="pending",
                report_id=None,
                _report_snapshot=None,
                error=None,
            )
            changed = True
        news = routine.get("news_context")
        if news and news.get("status") in {"failed", "interrupted"}:
            news.update(
                status="pending",
                source_status="pending",
                items=[],
                item_count=0,
                errors=[],
            )
            changed = True
        screening = routine.get("strategy_screening")
        if screening and screening.get("status") in {"failed", "interrupted", "blocked"}:
            screening_status = screening.get("status")
            screening.update(status="pending", candidate_count=0, error=None)
            if screening_status != "blocked":
                routine["candidates"] = []
            changed = True
        for target_type, target_items in (
            ("position", routine.get("positions", [])),
            ("candidate", routine.get("candidates", [])),
        ):
            for item in target_items:
                if daily_analysis_graph.needs_upgrade(item.get("graph")):
                    item.update(
                        status="pending",
                        graph=daily_analysis_graph.new_graph(
                            target_type=target_type,
                            target_ref=item["source_ref"],
                            symbol=item["symbol"],
                        ),
                        report_id=None,
                        _report_snapshot=None,
                        error=None,
                    )
                    changed = True
                    continue
                if item["status"] in {"failed", "interrupted", "blocked"}:
                    graph = item.get("graph")
                    if graph and item["status"] != "blocked":
                        daily_analysis_graph.prepare_failed_retry(graph)
                    item.update(
                        status="pending",
                        report_id=None,
                        _report_snapshot=None,
                        error=None,
                    )
                    changed = True
        if changed:
            routine["status"] = "running"
            routine["cancel_requested_at"] = None
            routine["updated_at"] = _now_iso()
            _write_all(items)
        selected = copy.deepcopy(routine)
    return _hydrate(selected), changed


def _summary_status(routine: dict, incomplete_status: str) -> str:
    if routine["market_review"]["status"] in {"failed", "interrupted"}:
        return "failed"
    statuses = [routine["market_review"]["status"]]
    news = routine.get("news_context")
    if news and news.get("status") != "skipped":
        statuses.append(news["status"])
    screening = routine.get("strategy_screening")
    if screening and screening.get("status") != "skipped":
        statuses.append(screening["status"])
    statuses.extend(
        item["status"]
        for item in [*routine.get("positions", []), *routine.get("candidates", [])]
    )
    completed = sum(status == "completed" for status in statuses)
    failed = sum(status in {"failed", "interrupted", "blocked"} for status in statuses)
    if completed == len(statuses):
        return "completed"
    if failed == len(statuses):
        return "failed"
    if completed + failed == len(statuses):
        return "degraded"
    return incomplete_status


def _mark_routine_interrupted(
    routine: dict,
    *,
    requested_at: str,
    force: bool = False,
) -> bool:
    """把一个运行实例及其所有尚未结束的子任务冻结为中断状态。"""
    changed = False
    message = "用户已中断全部进行中的每日复盘任务"
    for section_name in ("market_review", "news_context", "strategy_screening"):
        section = routine.get(section_name)
        if section and section.get("status") in {"pending", "running"}:
            section["status"] = "interrupted"
            if section_name != "news_context":
                section["error"] = message
            changed = True
    for target in [*routine.get("positions", []), *routine.get("candidates", [])]:
        graph = target.get("graph")
        if graph and daily_analysis_graph.mark_interrupted(graph):
            target["error"] = daily_analysis_graph.graph_error(graph) or message
            changed = True
        if target.get("status") in {"pending", "running"}:
            target["status"] = "interrupted"
            target["error"] = target.get("error") or message
            changed = True
    if changed or (force and routine.get("status") == "running"):
        routine["status"] = "interrupted"
        routine["cancel_requested_at"] = requested_at
        routine["updated_at"] = requested_at
    return changed


async def interrupt_all() -> list[str]:
    """取消全部活跃复盘协程,并把尚未结束的持久化工作统一标记为中断。"""
    requested_at = _now_iso()
    with _LOCK:
        items = _read_all()
        active_ids = {
            str(item.get("id"))
            for item in items
            if item.get("status") == "running" and item.get("id")
        }
        active_ids.update(_ACTIVE_TASKS)
        interrupted_ids: list[str] = []
        for routine in items:
            routine_id = str(routine.get("id") or "")
            if routine_id not in active_ids:
                continue
            if _mark_routine_interrupted(
                routine,
                requested_at=requested_at,
                force=True,
            ):
                interrupted_ids.append(routine_id)
        if interrupted_ids:
            _write_all(items)
        tasks = [
            task
            for routine_id, task in _ACTIVE_TASKS.items()
            if routine_id in active_ids and not task.done()
        ]
    current = asyncio.current_task()
    for task in tasks:
        if task is not current:
            task.cancel()
    await asyncio.sleep(0)
    return interrupted_ids


def recover_interrupted() -> int:
    """服务启动时将不可能继续运行的内存任务显式标记为中断。"""
    recovered = 0
    with _LOCK:
        items = _read_all()
        for routine in items:
            if _mark_routine_interrupted(routine, requested_at=_now_iso()):
                recovered += 1
                continue
            if routine.get("status") != "running":
                continue
            routine["status"] = _summary_status(routine, "interrupted")
            routine["updated_at"] = _now_iso()
            recovered += 1
        if recovered:
            _write_all(items)
    return recovered


def _update_market(business_date: str, **patch) -> None:
    with _LOCK:
        items = _read_all()
        routine = _find(items, business_date)
        if not routine:
            return
        if routine.get("cancel_requested_at"):
            return
        routine["market_review"].update(patch)
        routine["updated_at"] = _now_iso()
        _write_all(items)


def _update_news(business_date: str, **patch) -> None:
    with _LOCK:
        items = _read_all()
        routine = _find(items, business_date)
        if not routine:
            return
        if routine.get("cancel_requested_at"):
            return
        routine.setdefault("news_context", {}).update(patch)
        routine["updated_at"] = _now_iso()
        _write_all(items)


async def _run_news(app_state, business_date: str) -> None:
    """抓取一次市场新闻并冻结到复盘档案;所有查询都受 business_date 截止约束。"""
    _update_news(business_date, status="running", source_status="running", errors=[])
    as_of_key = _business_date(business_date)
    as_of = date.fromisoformat(as_of_key)
    try:
        context = await review_news.collect_review_news(_data_dir(app_state), as_of)
        with _LOCK:
            items = _read_all()
            routine = _find(items, business_date)
            if not routine:
                return
            if routine.get("cancel_requested_at"):
                return
            routine["news_context"] = context
            for target in routine.get("positions", []):
                target["news"] = review_news.query_items(
                    _data_dir(app_state),
                    as_of=as_of,
                    symbol=target.get("symbol"),
                    name=target.get("name"),
                    limit=12,
                )
            routine["updated_at"] = _now_iso()
            _write_all(items)
    except Exception as exc:  # 新闻缺失不能阻断行情和多 Agent 研究
        _update_news(
            business_date,
            status="completed",
            source_status="failed",
            as_of=as_of_key,
            items=[],
            item_count=0,
            errors=[_clean_error(exc)],
        )


def _update_screening(business_date: str, **patch) -> None:
    with _LOCK:
        items = _read_all()
        routine = _find(items, business_date)
        if not routine:
            return
        if routine.get("cancel_requested_at"):
            return
        routine["strategy_screening"].update(patch)
        routine["updated_at"] = _now_iso()
        _write_all(items)


def _replace_candidates(business_date: str, candidates: list[dict], **screening_patch) -> None:
    with _LOCK:
        items = _read_all()
        routine = _find(items, business_date)
        if not routine:
            return
        if routine.get("cancel_requested_at"):
            return
        routine["candidates"] = candidates
        routine["strategy_screening"].update(screening_patch)
        routine["updated_at"] = _now_iso()
        _write_all(items)


def _block_screening(business_date: str, message: str) -> None:
    with _LOCK:
        items = _read_all()
        routine = _find(items, business_date)
        if not routine:
            return
        if routine.get("cancel_requested_at"):
            return
        screening = routine.get("strategy_screening", {})
        if screening.get("status") in {"pending", "running", "blocked"}:
            screening.update(status="blocked", error=message)
            routine["updated_at"] = _now_iso()
            _write_all(items)


def _set_target_blocked(
    business_date: str,
    target_type: str,
    message: str,
) -> None:
    with _LOCK:
        items = _read_all()
        routine = _find(items, business_date)
        if not routine:
            return
        if routine.get("cancel_requested_at"):
            return
        changed = False
        for target in _target_list(routine, target_type):
            if target.get("status") not in {"pending", "running", "blocked"}:
                continue
            target.update(status="blocked", error=message)
            changed = True
        if changed:
            routine["updated_at"] = _now_iso()
            _write_all(items)


def _release_blocked_targets(business_date: str, target_type: str) -> None:
    with _LOCK:
        items = _read_all()
        routine = _find(items, business_date)
        if not routine:
            return
        if routine.get("cancel_requested_at"):
            return
        changed = False
        for target in _target_list(routine, target_type):
            if target.get("status") != "blocked":
                continue
            target.update(status="pending", error=None)
            changed = True
        if changed:
            routine["updated_at"] = _now_iso()
            _write_all(items)


def _target_list(routine: dict, target_type: str) -> list[dict]:
    return routine["positions"] if target_type == "position" else routine["candidates"]


def _update_target(
    business_date: str,
    target_type: str,
    source_ref: str,
    **patch,
) -> None:
    with _LOCK:
        items = _read_all()
        routine = _find(items, business_date)
        if not routine:
            return
        if routine.get("cancel_requested_at"):
            return
        target = next(
            (
                item
                for item in _target_list(routine, target_type)
                if item["source_ref"] == source_ref
            ),
            None,
        )
        if target:
            target.update(patch)
            routine["updated_at"] = _now_iso()
            _write_all(items)


def _clean_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:500]


async def _run_market(app_state, business_date: str) -> None:
    _update_market(business_date, status="running", error=None)
    try:
        routine = _get_raw(business_date) or {}
        as_of_key = str(routine.get("business_date") or business_date)
        existing = next(
            (
                item
                for item in market_recap_reports.list_reports()
                if item.get("source") == "daily_review"
                and item.get("daily_review_id") == routine.get("id")
                and item.get("point_in_time_version") == 1
            ),
            None,
        )
        if existing:
            _update_market(
                business_date,
                status="completed",
                report_id=existing["id"],
                _report_snapshot=existing,
                error=None,
            )
            return
        content, meta = await recap_market_once(
            app_state.repo,
            getattr(app_state, "quote_service", None),
            getattr(app_state, "depth_service", None),
            date.fromisoformat(as_of_key),
            news=list(routine.get("news_context", {}).get("items") or []),
        )
        if not content:
            raise RuntimeError("大盘复盘未生成有效内容")
        report = market_recap_reports.save_report(
            {
                "as_of": as_of_key,
                "focus": "",
                "content": content,
                "summary": meta.get("summary", ""),
                "emotion_score": meta.get("emotion_score"),
                "emotion_label": meta.get("emotion_label", ""),
                "source": "daily_review",
                "daily_review_id": routine.get("id"),
                "point_in_time_version": 1,
                "news_cutoff_at": routine.get("news_context", {}).get("cutoff_at"),
            }
        )
        _update_market(
            business_date,
            status="completed",
            report_id=report["id"],
            _report_snapshot=report,
            error=None,
        )
    except Exception as exc:
        _update_market(business_date, status="failed", error=_clean_error(exc))


def _position_context(item: dict) -> dict:
    context = {
        field: item.get(field)
        for field in (
            "source_ref",
            "account_id",
            "account_name",
            "symbol",
            "quantity",
            "average_cost",
            "purchase_date",
            "total_cost",
            "current_price",
            "price_date",
            "market_value",
            "unrealized_pnl",
            "unrealized_return_ratio",
            "note",
        )
    }
    context["news_evidence"] = list(item.get("news") or [])
    return context


def _candidate_context(item: dict) -> dict:
    return {
        "source_ref": item.get("source_ref"),
        "symbol": item.get("symbol"),
        "rank": item.get("rank"),
        "matched_strategies": item.get("matched_strategies") or [],
        "reason": item.get("reason"),
        "news_evidence": list(item.get("news") or []),
    }


def _data_dir(app_state) -> Path:
    if hasattr(app_state.repo, "store"):
        return app_state.repo.store.data_dir
    return settings.data_dir


def _screen_candidates(
    app_state,
    business_date: str,
    requested_strategy_ids: list[str] | None,
) -> tuple[list[dict], list[dict]]:
    engine = getattr(app_state, "strategy_engine", None)
    if engine is None:
        raise RuntimeError("策略引擎未初始化")

    metadata = [
        item
        for item in engine.list_strategies()
        if "stock" in item.get("asset_types", ["stock"])
        and "1d" in item.get("timeframes", ["1d"])
    ]
    metadata_by_id = {str(item["id"]): item for item in metadata}
    if requested_strategy_ids is None:
        selected_metadata = metadata
    else:
        unavailable = [
            strategy_id
            for strategy_id in requested_strategy_ids
            if strategy_id not in metadata_by_id
        ]
        if unavailable:
            raise RuntimeError(f"策略池包含不可用的股票日线策略: {'、'.join(unavailable)}")
        selected_metadata = [metadata_by_id[strategy_id] for strategy_id in requested_strategy_ids]

    strategies = [
        {
            "id": str(item["id"]),
            "name": str(item.get("name") or item["id"]),
        }
        for item in selected_metadata
    ]
    strategy_ids = [item["id"] for item in strategies]
    if requested_strategy_ids is None and not strategy_ids:
        raise RuntimeError("没有可用于 A 股日线复盘的策略")
    if not strategy_ids:
        return [], []

    data_dir = _data_dir(app_state)
    overrides_map = {
        strategy_id: strategy_config.load_override(data_dir, strategy_id)
        for strategy_id in strategy_ids
    }
    service = ScreenerService(app_state.repo, asset_type="stock")
    as_of = date.fromisoformat(business_date)
    context = service.build_strategy_context(
        engine,
        as_of,
        strategy_ids,
        overrides_map=overrides_map,
    )
    results = engine.run_all(
        context,
        overrides_map=overrides_map,
        strategy_ids=strategy_ids,
    )
    meta_by_id = {str(item["id"]): item for item in selected_metadata}
    grouped: dict[str, dict] = {}
    for strategy_id in strategy_ids:
        result = results.get(strategy_id)
        if result is None:
            continue
        rows = list(result.rows or [])
        strategy_meta = meta_by_id[strategy_id]
        for index, row in enumerate(rows):
            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                continue
            bucket = grouped.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "name": str(row.get("name") or ""),
                    "matched_strategies": [],
                },
            )
            if not bucket["name"] and row.get("name"):
                bucket["name"] = str(row["name"])
            raw_score = result.scores.get(symbol) if getattr(result, "scores", None) else None
            bucket["matched_strategies"].append(
                {
                    "id": strategy_id,
                    "name": strategy_meta.get("name") or strategy_id,
                    "rank": index + 1,
                    "score": raw_score,
                }
            )

    # dict 保留首次插入顺序。策略池中的策略顺序优先。同一策略内沿用 Screener
    # 返回顺序。同一股票再次命中时只追加来源证据。不参与投票或重新排序。
    selected = list(grouped.values())[:_MAX_CANDIDATES]
    missing_names = [item["symbol"] for item in selected if not item["name"]]
    name_map = app_state.repo.get_name_map(missing_names) if missing_names else {}
    candidates: list[dict] = []
    for rank, item in enumerate(selected, start=1):
        symbol = item["symbol"]
        matched_strategies = item["matched_strategies"]
        first_match = matched_strategies[0]
        source_ref = f"candidate:{symbol}"
        candidates.append(
            {
                "source_ref": source_ref,
                "symbol": symbol,
                "name": item["name"] or name_map.get(symbol, ""),
                "asset_type": "stock",
                "rank": rank,
                "matched_strategies": matched_strategies,
                "reason": (
                    f"按策略池顺序首次来自“{first_match['name']}”第 "
                    f"{first_match['rank']} 名。共命中 {len(matched_strategies)} 个策略"
                ),
                "news": review_news.query_items(
                    data_dir,
                    as_of=as_of,
                    symbol=symbol,
                    name=item["name"] or name_map.get(symbol, ""),
                    limit=12,
                ),
                "status": "pending",
                "graph": daily_analysis_graph.new_graph(
                    target_type="candidate",
                    target_ref=source_ref,
                    symbol=symbol,
                ),
                "report_id": None,
                "_report_snapshot": None,
                "error": None,
            }
        )
    return candidates, strategies


async def _run_screening(app_state, business_date: str) -> None:
    _update_screening(business_date, status="running", error=None)
    try:
        routine = _get_raw(business_date) or {}
        as_of_key = str(routine.get("business_date") or business_date)
        screening = routine.get("strategy_screening", {})
        selection_source = screening.get("selection_source", "all_available")
        requested_strategy_ids = (
            list(screening.get("strategy_ids") or [])
            if selection_source == "screener_pool"
            else None
        )
        candidates, strategies = await asyncio.to_thread(
            _screen_candidates,
            app_state,
            as_of_key,
            requested_strategy_ids,
        )
        _replace_candidates(
            business_date,
            candidates,
            status="completed",
            selection_source=selection_source,
            strategy_ids=[item["id"] for item in strategies],
            strategies=strategies,
            candidate_count=len(candidates),
            selection_method=_SELECTION_METHOD,
            error=None,
        )
    except Exception as exc:
        _update_screening(
            business_date,
            status="failed",
            candidate_count=0,
            error=_clean_error(exc),
        )


def _find_target(business_date: str, target_type: str, source_ref: str) -> dict | None:
    routine = _get_raw(business_date)
    if not routine:
        return None
    return next(
        (
            item
            for item in _target_list(routine, target_type)
            if item.get("source_ref") == source_ref
        ),
        None,
    )


async def _run_target(
    app_state,
    business_date: str,
    target_type: str,
    source_ref: str,
    semaphore,
) -> None:
    async with semaphore:
        routine = _get_raw(business_date) or {}
        routine_id = str(routine.get("id") or business_date)
        as_of_key = str(routine.get("business_date") or business_date)
        item = _find_target(business_date, target_type, source_ref)
        if item is None:
            return
        graph = item.get("graph") or daily_analysis_graph.new_graph(
            target_type=target_type,
            target_ref=source_ref,
            symbol=item["symbol"],
        )
        _update_target(
            business_date,
            target_type,
            source_ref,
            status="running",
            graph=graph,
            error=None,
        )
        try:
            existing = next(
                (
                    report
                    for report in stock_reports.list_reports()
                    if report.get("source") == "daily_review"
                    and report.get("daily_review_id") == routine_id
                    and report.get("source_ref") == source_ref
                    and report.get("analysis_graph_id") == graph["id"]
                ),
                None,
            )
            if existing and graph.get("status") == "completed":
                _update_target(
                    business_date,
                    target_type,
                    source_ref,
                    status="completed",
                    report_id=existing["id"],
                    _report_snapshot=existing,
                    error=None,
                )
                return

            target_context = (
                _position_context(item)
                if target_type == "position"
                else _candidate_context(item)
            )
            graph = await daily_analysis_graph.run_graph(
                graph,
                repo=app_state.repo,
                data_dir=_data_dir(app_state),
                target_context=target_context,
                as_of=date.fromisoformat(as_of_key),
                on_update=lambda updated: _update_target(
                    business_date,
                    target_type,
                    source_ref,
                    graph=updated,
                ),
            )
            if graph.get("status") != "completed":
                _update_target(
                    business_date,
                    target_type,
                    source_ref,
                    status="failed",
                    graph=graph,
                    error=daily_analysis_graph.graph_error(graph),
                )
                return

            report_payload = daily_analysis_graph.graph_report(graph)
            report = stock_reports.save_report(
                {
                    "id": f"sar_daily_{uuid.uuid4().hex}",
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "focus": (
                        "每日复盘中的持仓多 Agent 客观分析"
                        if target_type == "position"
                        else "每日复盘中的策略候选多 Agent 客观分析"
                    ),
                    **report_payload,
                    "source": "daily_review",
                    "source_ref": source_ref,
                    "daily_review_id": routine_id,
                    "daily_review_date": as_of_key,
                    "target_type": target_type,
                    "analysis_graph_id": graph["id"],
                    "point_in_time_version": 1,
                    "news_cutoff_at": (
                        (_get_raw(business_date) or {}).get("news_context", {}).get(
                            "cutoff_at"
                        )
                    ),
                }
            )
            _update_target(
                business_date,
                target_type,
                source_ref,
                status="completed",
                graph=graph,
                report_id=report["id"],
                _report_snapshot=report,
                error=None,
            )
        except Exception as exc:
            _update_target(
                business_date,
                target_type,
                source_ref,
                status="failed",
                error=_clean_error(exc),
            )


def prepare_node_retry(
    routine_ref: date | str,
    *,
    target_type: str,
    source_ref: str,
    node_id: str,
) -> tuple[dict | None, bool, str | None]:
    key = routine_ref.isoformat() if isinstance(routine_ref, date) else routine_ref
    if target_type not in {"position", "candidate"}:
        return get_routine(key), False, "不支持的分析目标类型"
    with _LOCK:
        items = _read_all()
        routine = _find(items, key)
        if not routine:
            return None, False, "每日复盘档案不存在"
        target = next(
            (
                item
                for item in _target_list(routine, target_type)
                if item.get("source_ref") == source_ref
            ),
            None,
        )
        if target is None:
            return _hydrate(copy.deepcopy(routine)), False, "分析目标不存在"
        graph = target.get("graph")
        if daily_analysis_graph.needs_upgrade(graph):
            return _hydrate(copy.deepcopy(routine)), False, "历史分析需要先升级为当前研究 Graph"
        try:
            changed = daily_analysis_graph.prepare_node_retry(graph, node_id)
        except KeyError as exc:
            return _hydrate(copy.deepcopy(routine)), False, str(exc)
        if not changed:
            return _hydrate(copy.deepcopy(routine)), False, "只能恢复失败或中断节点"
        target.update(
            status="pending",
            report_id=None,
            _report_snapshot=None,
            error=None,
        )
        routine["status"] = "running"
        routine["cancel_requested_at"] = None
        routine["updated_at"] = _now_iso()
        _write_all(items)
        selected = copy.deepcopy(routine)
    return _hydrate(selected), True, None


async def run_target_graph(
    app_state,
    business_date: str,
    target_type: str,
    source_ref: str,
) -> None:
    await _run_target(
        app_state,
        business_date,
        target_type,
        source_ref,
        asyncio.Semaphore(1),
    )
    _finalize(business_date)


def _finalize(business_date: str) -> None:
    routine = _get_raw(business_date)
    if not routine:
        return
    if routine.get("cancel_requested_at"):
        return
    routine["status"] = _summary_status(routine, "running")
    _upsert(routine)


async def _run_routine_steps(app_state, business_date: str) -> None:
    routine = _get_raw(business_date)
    if not routine:
        return
    # 每一步都读取同一份按日期截断的冻结证据,并且只有前一步成功后
    # 才释放下一步,保证页面步骤条就是实际运行状态机。
    if routine.get("news_context", {}).get("status") == "pending":
        await _run_news(app_state, business_date)
        routine = _get_raw(business_date) or routine

    if routine["market_review"]["status"] == "pending":
        await _run_market(app_state, business_date)
    routine = _get_raw(business_date) or routine
    if routine["market_review"]["status"] != "completed":
        _block_screening(business_date, _MARKET_BLOCKED_MESSAGE)
        _set_target_blocked(business_date, "candidate", _MARKET_BLOCKED_MESSAGE)
        _set_target_blocked(business_date, "position", _MARKET_BLOCKED_MESSAGE)
        _finalize(business_date)
        return

    if routine.get("strategy_screening", {}).get("status") == "pending":
        await _run_screening(app_state, business_date)
    routine = _get_raw(business_date) or routine
    if routine.get("strategy_screening", {}).get("status") != "completed":
        _set_target_blocked(business_date, "position", _CANDIDATE_BLOCKED_MESSAGE)
        _finalize(business_date)
        return

    semaphore = asyncio.Semaphore(3)
    candidate_tasks = [
        asyncio.create_task(
            _run_target(
                app_state,
                business_date,
                "candidate",
                item["source_ref"],
                semaphore,
            )
        )
        for item in routine.get("candidates", [])
        if item["status"] == "pending"
    ]
    if candidate_tasks:
        await asyncio.gather(*candidate_tasks)
    routine = _get_raw(business_date) or routine
    if any(item["status"] != "completed" for item in routine.get("candidates", [])):
        _set_target_blocked(business_date, "position", _CANDIDATE_BLOCKED_MESSAGE)
        _finalize(business_date)
        return

    _release_blocked_targets(business_date, "position")
    routine = _get_raw(business_date) or routine
    position_tasks = [
        asyncio.create_task(
            _run_target(
                app_state,
                business_date,
                "position",
                item["source_ref"],
                semaphore,
            )
        )
        for item in routine.get("positions", [])
        if item["status"] == "pending"
    ]
    if position_tasks:
        await asyncio.gather(*position_tasks)
    _finalize(business_date)


async def run_routine(app_state, routine_ref: str) -> None:
    """执行单个复盘实例,并注册可被“中断全部”取消的真实协程任务。"""
    routine = _get_raw(routine_ref)
    if not routine:
        return
    routine_id = str(routine["id"])
    task = asyncio.current_task()
    if task is not None:
        with _LOCK:
            _ACTIVE_TASKS[routine_id] = task
    try:
        await _run_routine_steps(app_state, routine_id)
    except asyncio.CancelledError:
        with _LOCK:
            items = _read_all()
            current = _find(items, routine_id)
            is_current_task = _ACTIVE_TASKS.get(routine_id) is task
            if current and is_current_task and _mark_routine_interrupted(
                current,
                requested_at=_now_iso(),
                force=True,
            ):
                _write_all(items)
        return
    finally:
        if task is not None:
            with _LOCK:
                if _ACTIVE_TASKS.get(routine_id) is task:
                    _ACTIVE_TASKS.pop(routine_id, None)
