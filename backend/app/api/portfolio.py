"""研究型账户、交易流水与历史持仓快照 API。"""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from datetime import date
from math import isfinite
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.api.monitor_rules import sync_engine
from app.config import settings
from app.services import (
    portfolio,
    portfolio_price_monitors,
    preferences,
    statement_import,
    watchlist,
)
from app.services.stock_analyzer import analyze_stock_stream
from app.strategy import monitor_rules

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])
logger = logging.getLogger(__name__)
_MONITOR_ENGINE_SYNC_RETRY_INITIAL_SECONDS = 0.25
_MONITOR_ENGINE_SYNC_RETRY_MAX_SECONDS = 30.0
_MONITOR_ENGINE_SYNC_RETRY_SHUTDOWN_TIMEOUT_SECONDS = 1.0
_MONITOR_ENGINE_SYNC_RETRY_STATE_LOCK = threading.Lock()
_MONITOR_ENGINE_SYNC_RETRY_STATE_ATTR = "_portfolio_monitor_engine_sync_retry"


class _MonitorEngineSyncRetryState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.generation = 0
        self.worker: threading.Thread | None = None


class AccountWriteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("账户名称不能为空")
        return clean


class TradeCreateRequest(BaseModel):
    account_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1, max_length=20)
    trade_date: date
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0, allow_inf_nan=False)
    price: float = Field(ge=0, allow_inf_nan=False)
    # 留空(None)表示按费率配置自动估算
    fee: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tax: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    note: str = Field(default="", max_length=500)
    insert_before_trade_id: str | None = Field(default=None, min_length=1)

    @field_validator("quantity", "price", "fee", "tax", mode="before")
    @classmethod
    def validate_finite_number(cls, value):
        if value is None:
            return value
        try:
            finite = isfinite(float(value))
        except (TypeError, ValueError):
            return value
        if not finite:
            raise HTTPException(status_code=422, detail="数量、价格和费用必须为有限数字")
        return value


class TradeEstimateRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0, allow_inf_nan=False)
    price: float = Field(ge=0, allow_inf_nan=False)


class StatementCommitItem(BaseModel):
    mode: Literal["insert", "calibrate"]
    matched_trade_id: str | None = None
    symbol: str | None = None
    trade_date: date | None = None
    side: Literal["buy", "sell"] | None = None
    quantity: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    fee: float = Field(default=0, ge=0, allow_inf_nan=False)
    tax: float = Field(default=0, ge=0, allow_inf_nan=False)
    note: str = Field(default="", max_length=500)


class StatementCommitRequest(BaseModel):
    account_id: str = Field(min_length=1)
    items: list[StatementCommitItem] = Field(min_length=1, max_length=2000)


class PositionAnalyzeRequest(BaseModel):
    focus: str = Field(default="", max_length=500)
    as_of: date | None = None


class PositionPriceMonitorRequest(BaseModel):
    name: str = Field(default="", max_length=80)
    asset_type: Literal["stock", "etf"]
    stop_loss_price: float = Field(gt=0, allow_inf_nan=False)
    add_position_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    webhook_channels: list[Literal["feishu", "wecom"]] = Field(default_factory=list)


class WatchPoolCreateRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)


class TradeReorderRequest(BaseModel):
    trade_ids: list[str] = Field(min_length=2, max_length=200)


class TradePriceUpdateRequest(BaseModel):
    price: float = Field(gt=0, allow_inf_nan=False)


class TradeExecutionUpdateRequest(BaseModel):
    quantity: float = Field(gt=0, allow_inf_nan=False)
    price: float = Field(gt=0, allow_inf_nan=False)


class TradeDateUpdateRequest(BaseModel):
    trade_date: date


class TradeCostUpdateRequest(BaseModel):
    fee: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tax: float | None = Field(default=None, ge=0, allow_inf_nan=False)


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, portfolio.PortfolioNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, portfolio.PortfolioConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="持仓操作失败")


def _monitor_engine_sync_retry_state(request: Request) -> _MonitorEngineSyncRetryState:
    app_state = request.app.state
    with _MONITOR_ENGINE_SYNC_RETRY_STATE_LOCK:
        state = getattr(app_state, _MONITOR_ENGINE_SYNC_RETRY_STATE_ATTR, None)
        if state is None:
            state = _MonitorEngineSyncRetryState()
            setattr(app_state, _MONITOR_ENGINE_SYNC_RETRY_STATE_ATTR, state)
        return state


def _retry_monitor_engine_sync(
    request: Request,
    state: _MonitorEngineSyncRetryState,
) -> None:
    delay = _MONITOR_ENGINE_SYNC_RETRY_INITIAL_SECONDS
    worker = threading.current_thread()
    generation = 0
    released = False
    try:
        while not state.stop_event.wait(delay):
            with state.lock:
                if state.stop_event.is_set():
                    return
                generation = state.generation
            try:
                sync_engine(request)
            except Exception:
                if state.stop_event.is_set():
                    return
                logger.exception("closed position monitor engine sync retry failed")
                delay = min(delay * 2, _MONITOR_ENGINE_SYNC_RETRY_MAX_SECONDS)
                continue
            with state.lock:
                if state.generation == generation:
                    if state.worker is worker:
                        state.worker = None
                    released = True
                    return
            delay = _MONITOR_ENGINE_SYNC_RETRY_INITIAL_SECONDS
    finally:
        if not released:
            with state.lock:
                if state.worker is worker:
                    state.worker = None
                    if not state.stop_event.is_set():
                        try:
                            _start_monitor_engine_sync_retry_worker(request, state)
                        except Exception:
                            logger.exception(
                                "monitor engine sync retry worker handoff failed"
                            )


def _start_monitor_engine_sync_retry_worker(
    request: Request,
    state: _MonitorEngineSyncRetryState,
) -> None:
    """在持有 state.lock 时创建并登记唯一 worker。"""
    worker = threading.Thread(
        target=_retry_monitor_engine_sync,
        args=(request, state),
        name="monitor-engine-sync-retry",
        daemon=True,
    )
    state.worker = worker
    try:
        worker.start()
    except Exception:
        state.worker = None
        raise


def _schedule_monitor_engine_sync_retry(request: Request) -> None:
    """每个应用仅保留一个重试线程,并确保新一代变更不会丢失。"""
    state = _monitor_engine_sync_retry_state(request)
    with state.lock:
        if state.stop_event.is_set():
            return
        state.generation += 1
        if state.worker is not None:
            return
        _start_monitor_engine_sync_retry_worker(request, state)


def reset_monitor_engine_sync_retry(app) -> None:
    """每次 lifespan 启动时为该应用安装全新的重试状态。"""
    state = _MonitorEngineSyncRetryState()
    with _MONITOR_ENGINE_SYNC_RETRY_STATE_LOCK:
        previous = getattr(app.state, _MONITOR_ENGINE_SYNC_RETRY_STATE_ATTR, None)
        if previous is not None:
            previous.stop_event.set()
        setattr(app.state, _MONITOR_ENGINE_SYNC_RETRY_STATE_ATTR, state)


def stop_monitor_engine_sync_retry(app) -> None:
    """应用关闭时唤醒并回收该应用的监控规则同步重试线程。"""
    with _MONITOR_ENGINE_SYNC_RETRY_STATE_LOCK:
        state = getattr(app.state, _MONITOR_ENGINE_SYNC_RETRY_STATE_ATTR, None)
    if state is None:
        return
    state.stop_event.set()
    with state.lock:
        worker = state.worker
    if worker is None or worker is threading.current_thread():
        return
    worker.join(timeout=_MONITOR_ENGINE_SYNC_RETRY_SHUTDOWN_TIMEOUT_SECONDS)
    if worker.is_alive():
        logger.warning("monitor engine sync retry worker did not stop before timeout")


def _cleanup_closed_position_rules(request: Request, held_before: set[str]) -> None:
    """交易落盘后清理规则,失败不得把已成功交易报告为失败。"""
    rules_changed = False
    try:
        if not held_before:
            return
        closed_symbols = held_before - portfolio.held_symbols()
        if not closed_symbols:
            return
        store = getattr(request.app.state.repo, "store", None)
        data_dir = getattr(store, "data_dir", settings.data_dir)
        with monitor_rules.locked():
            rules_changed = bool(
                monitor_rules.delete_for_symbols(data_dir, closed_symbols)
            )
            if rules_changed:
                sync_engine(request)
    except Exception:
        logger.exception("closed position monitor rule cleanup failed")
        if rules_changed:
            try:
                _schedule_monitor_engine_sync_retry(request)
            except Exception:
                logger.exception("closed position monitor engine sync retry scheduling failed")


@router.get("/accounts")
def list_accounts():
    return {"items": portfolio.list_accounts()}


@router.post("/accounts", status_code=status.HTTP_201_CREATED)
def create_account(body: AccountWriteRequest):
    return portfolio.create_account(body.name)


@router.patch("/accounts/{account_id}")
def update_account(account_id: str, body: AccountWriteRequest):
    try:
        return portfolio.update_account(account_id, body.name)
    except Exception as exc:
        raise _map_error(exc) from exc


@router.delete("/accounts/{account_id}")
def delete_account(account_id: str):
    try:
        portfolio.delete_account(account_id)
    except Exception as exc:
        raise _map_error(exc) from exc
    return {"ok": True}


@router.get("/watch-pool")
def list_watch_pool():
    return {"items": portfolio.list_watch_pool()}


@router.post("/watch-pool", status_code=status.HTTP_201_CREATED)
def add_watch_pool_item(body: WatchPoolCreateRequest, request: Request):
    try:
        item = portfolio.add_watch_pool_item(request.app.state.repo, body.symbol)
        try:
            existing = {entry["symbol"] for entry in watchlist.list_symbols()}
            if item["symbol"] not in existing:
                watchlist.add(item["symbol"])
        except Exception:
            portfolio.remove_watch_pool_item(item["symbol"])
            raise
        return item
    except Exception as exc:
        raise _map_error(exc) from exc


@router.delete("/watch-pool/{symbol}")
def remove_watch_pool_item(symbol: str):
    try:
        portfolio.remove_watch_pool_item(symbol)
    except Exception as exc:
        raise _map_error(exc) from exc
    return {"ok": True}


@router.post("/trades", status_code=status.HTTP_201_CREATED)
def create_trade(body: TradeCreateRequest, request: Request):
    try:
        if body.trade_date > portfolio.today():
            raise ValueError("交易日期不能晚于今天")
        with portfolio.mutation_guard():
            result = portfolio.record_trade(
                request.app.state.repo,
                account_id=body.account_id,
                symbol=body.symbol,
                trade_date=body.trade_date,
                side=body.side,
                quantity=body.quantity,
                price=body.price,
                fee=body.fee,
                tax=body.tax,
                note=body.note,
                insert_before_trade_id=body.insert_before_trade_id,
            )
            if body.side == "sell":
                _cleanup_closed_position_rules(request, {result["symbol"]})
            return result
    except Exception as exc:
        raise _map_error(exc) from exc


@router.post("/trades/estimate")
def estimate_trade(body: TradeEstimateRequest, request: Request):
    try:
        result = portfolio.estimate_trade_cost_for(
            request.app.state.repo,
            symbol=body.symbol,
            side=body.side,
            quantity=body.quantity,
            price=body.price,
        )
    except Exception as exc:
        raise _map_error(exc) from exc
    return {**result, "profile": preferences.get_trade_fee_profile()}


_STATEMENT_SUFFIXES = {".csv", ".xlsx", ".xls"}
_STATEMENT_MAX_BYTES = 12 * 1024 * 1024


@router.post("/statement-preview")
async def preview_statement(
    request: Request,
    account_id: str = Form(min_length=1),
    file: UploadFile = File(...),
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _STATEMENT_SUFFIXES:
        raise HTTPException(status_code=400, detail="仅支持 CSV / Excel 交割单文件")
    tmp_dir = Path(tempfile.mkdtemp(prefix="statement-"))
    try:
        tmp_path = tmp_dir / f"statement{suffix}"
        size = 0
        with tmp_path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > _STATEMENT_MAX_BYTES:
                    raise HTTPException(status_code=400, detail="文件超过 12MB 上限")
                handle.write(chunk)
        try:
            parsed = statement_import.parse_statement_file(tmp_path)
        except statement_import.StatementParseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    items = [item for item in parsed["items"] if item.get("symbol")]
    unresolved = [item for item in parsed["items"] if not item.get("symbol")]
    try:
        result = portfolio.preview_statement(request.app.state.repo, account_id, items)
    except Exception as exc:
        raise _map_error(exc) from exc
    return {
        **result,
        "skipped_rows": parsed["skipped_rows"],
        "unresolved": [
            {"raw_code": item["raw_code"], "name": item["name"], "trade_date": item["trade_date"]}
            for item in unresolved
        ],
    }


@router.post("/statement-commit")
def commit_statement(body: StatementCommitRequest, request: Request):
    try:
        inserted_sell_symbols = {
            str(item.symbol).strip().upper()
            for item in body.items
            if item.mode == "insert" and item.side == "sell" and item.symbol
        }
        with portfolio.mutation_guard():
            held_before = (
                portfolio.held_symbols().intersection(inserted_sell_symbols)
                if inserted_sell_symbols
                else set()
            )
            result = portfolio.apply_statement(
                request.app.state.repo,
                body.account_id,
                [item.model_dump(mode="json") for item in body.items],
            )
            _cleanup_closed_position_rules(request, held_before)
            return result
    except Exception as exc:
        raise _map_error(exc) from exc


@router.get("/trades")
def list_trades(
    account_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
):
    try:
        return {
            "items": portfolio.list_trades(
                account_id=account_id,
                date_from=date_from,
                date_to=date_to,
            )
        }
    except Exception as exc:
        raise _map_error(exc) from exc


@router.delete("/trades/{trade_id}")
def delete_trade(trade_id: str, request: Request):
    try:
        with portfolio.mutation_guard():
            held_before = portfolio.held_symbols()
            portfolio.delete_trade(trade_id)
            _cleanup_closed_position_rules(request, held_before)
    except Exception as exc:
        raise _map_error(exc) from exc
    return {"ok": True}


@router.post("/trades/reorder")
def reorder_trades(body: TradeReorderRequest):
    try:
        portfolio.reorder_trades(body.trade_ids)
    except Exception as exc:
        raise _map_error(exc) from exc
    return {"ok": True}


@router.patch("/trades/{trade_id}")
def update_trade_execution(
    trade_id: str, body: TradeExecutionUpdateRequest, request: Request
):
    try:
        with portfolio.mutation_guard():
            held_before = portfolio.held_symbols()
            result = portfolio.update_trade_execution(
                trade_id,
                quantity=body.quantity,
                price=body.price,
            )
            _cleanup_closed_position_rules(request, held_before)
            return result
    except Exception as exc:
        raise _map_error(exc) from exc


@router.patch("/trades/{trade_id}/price")
def update_trade_price(trade_id: str, body: TradePriceUpdateRequest):
    try:
        return portfolio.update_trade_price(trade_id, body.price)
    except Exception as exc:
        raise _map_error(exc) from exc


@router.patch("/trades/{trade_id}/date")
def update_trade_date(
    trade_id: str, body: TradeDateUpdateRequest, request: Request
):
    try:
        with portfolio.mutation_guard():
            held_before = portfolio.held_symbols()
            result = portfolio.update_trade_date(trade_id, body.trade_date)
            _cleanup_closed_position_rules(request, held_before)
            return result
    except Exception as exc:
        raise _map_error(exc) from exc


@router.patch("/trades/{trade_id}/cost")
def update_trade_cost(trade_id: str, body: TradeCostUpdateRequest):
    try:
        return portfolio.update_trade_cost(trade_id, body.fee, body.tax)
    except Exception as exc:
        raise _map_error(exc) from exc


@router.post("/accounts/{account_id}/positions/{symbol}/analyze")
async def analyze_position(
    account_id: str,
    symbol: str,
    body: PositionAnalyzeRequest,
    request: Request,
):
    repo = request.app.state.repo
    try:
        context = portfolio.get_position_analysis_context(
            repo, account_id, symbol, as_of=body.as_of
        )
    except Exception as exc:
        raise _map_error(exc) from exc

    async def stream_gen():
        async for chunk in analyze_stock_stream(
            repo,
            repo.store.data_dir if hasattr(repo, "store") else settings.data_dir,
            context["symbol"],
            body.focus,
            portfolio_context=context,
            as_of=body.as_of,
        ):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/price-monitors")
def list_price_monitors(request: Request):
    data_dir = request.app.state.repo.store.data_dir
    return {"items": portfolio_price_monitors.list_monitors(data_dir)}


@router.put("/positions/{symbol}/price-monitor")
def save_price_monitor(symbol: str, body: PositionPriceMonitorRequest, request: Request):
    data_dir = request.app.state.repo.store.data_dir
    try:
        with monitor_rules.locked():
            item = portfolio_price_monitors.save_monitor(
                data_dir,
                symbol=symbol,
                name=body.name,
                asset_type=body.asset_type,
                stop_loss_price=body.stop_loss_price,
                add_position_price=body.add_position_price,
                webhook_channels=list(body.webhook_channels),
            )
            sync_engine(request)
        return item
    except Exception as exc:
        raise _map_error(exc) from exc


@router.get("/snapshot")
def get_snapshot(
    request: Request,
    as_of: date | None = None,
    account_id: str | None = None,
):
    try:
        return portfolio.get_snapshot(
            request.app.state.repo,
            as_of or portfolio.today(),
            account_id,
        )
    except Exception as exc:
        raise _map_error(exc) from exc
