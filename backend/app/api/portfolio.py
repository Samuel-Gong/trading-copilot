"""研究型账户、交易流水与历史持仓快照 API。"""
from __future__ import annotations

import shutil
import tempfile
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

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


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
        return portfolio.record_trade(
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
        )
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
        return portfolio.apply_statement(
            request.app.state.repo,
            body.account_id,
            [item.model_dump(mode="json") for item in body.items],
        )
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
def delete_trade(trade_id: str):
    try:
        portfolio.delete_trade(trade_id)
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
def update_trade_execution(trade_id: str, body: TradeExecutionUpdateRequest):
    try:
        return portfolio.update_trade_execution(
            trade_id,
            quantity=body.quantity,
            price=body.price,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


@router.patch("/trades/{trade_id}/price")
def update_trade_price(trade_id: str, body: TradePriceUpdateRequest):
    try:
        return portfolio.update_trade_price(trade_id, body.price)
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
