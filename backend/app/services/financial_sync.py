"""财务数据独立同步服务。

解耦于 K-line 管道, 自有调度 + 自有存储。
能力门控: Cap.FINANCIAL (Expert 套餐)
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from app.services.atomic_parquet import recover_file_set, replace_parquet_set
from app.services.provider_routes import provider_route_commit_guard
from app.services.update_slots import update_slot
from app.tickflow.capabilities import Cap, CapabilitySet

logger = logging.getLogger(__name__)

# 每个 API 请求最多 100 个标的
_BATCH_SIZE = 100

# 财务报表 + 历史股本表
FINANCIAL_TABLES = ("metrics", "income", "balance_sheet", "cash_flow", "shares")
_FINANCIAL_SNAPSHOT_LOCK = threading.RLock()
_PINNED_FINANCIAL_PROVIDER: ContextVar[tuple[str, object | None] | None] = (
    ContextVar("pinned_financial_provider", default=None)
)


def _financial_journal_path(data_dir: Path) -> Path:
    return data_dir / "financials" / ".financial_publish.json"


@contextmanager
def financial_snapshot_lock(data_dir: Path):
    """保护财务集合读写，并在读取前恢复未完成的跨文件发布。"""
    with _FINANCIAL_SNAPSHOT_LOCK:
        recover_file_set(_financial_journal_path(data_dir))
        yield


# ================================================================
# 同步函数
# ================================================================

def _get_symbols(data_dir: Path) -> list[str]:
    """从 instruments 表获取标的列表。"""
    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return []
    try:
        df = pl.read_parquet(inst_path, columns=["symbol"])
        return df["symbol"].to_list()
    except Exception as e:
        logger.warning("读取 instruments 失败: %s", e)
        return []


def _resolve_financial_provider() -> tuple[str, object | None]:
    """解析选定财务源，返回 (tickflow/custom/unavailable, provider)。"""
    from app.services import preferences
    provider_name = preferences.get_financial_provider()
    if provider_name == "tickflow":
        return ("tickflow", None)
    from app.data_providers import custom as custom_sources
    try:
        if not custom_sources.provider_has_dataset(provider_name, "financial"):
            logger.warning("selected financial provider %s is unavailable", provider_name)
            return ("unavailable", None)
        # 这里只判断路由可用性；实例必须在实际网络调用期间通过 lease_provider 固定。
        return ("custom", None)
    except Exception as exc:
        logger.warning("selected financial provider %s resolution failed: %s", provider_name, exc)
        return ("unavailable", None)


@contextmanager
def _financial_provider_lease():
    """固定一次同步使用的数据源实例与注册表世代。"""
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    provider_name = preferences.get_financial_provider()
    provider_kind, resolved_provider = _resolve_financial_provider()
    if provider_kind != "custom":
        token = _PINNED_FINANCIAL_PROVIDER.set((provider_kind, resolved_provider))
        try:
            yield provider_name, provider_kind, None
        finally:
            _PINNED_FINANCIAL_PROVIDER.reset(token)
        return

    provider_context = custom_sources.lease_provider(provider_name)
    try:
        provider, generation = provider_context.__enter__()
    except ValueError:
        # 测试桩或注册表刚被移除时仍保持 fail-closed，不回退 TickFlow。
        token = _PINNED_FINANCIAL_PROVIDER.set((provider_kind, resolved_provider))
        try:
            yield provider_name, provider_kind, None
        finally:
            _PINNED_FINANCIAL_PROVIDER.reset(token)
        return

    token = _PINNED_FINANCIAL_PROVIDER.set((provider_kind, provider))
    try:
        yield provider_name, provider_kind, generation
    finally:
        _PINNED_FINANCIAL_PROVIDER.reset(token)
        provider_context.__exit__(None, None, None)


def _validate_financial_provider_lease(
    provider_name: str,
    provider_kind: str,
    generation: int | None,
) -> None:
    """提交前确认路由与注册表未变化，避免跨源五表快照。"""
    from app.services import preferences

    if preferences.get_financial_provider() != provider_name:
        raise RuntimeError("financial provider changed during sync")
    if provider_kind == "custom" and generation is not None:
        from app.data_providers import custom as custom_sources

        if custom_sources.registry_generation() != generation:
            raise RuntimeError("financial provider configuration changed during sync")


def _financial_is_custom() -> bool:
    """兼容能力探测调用：当前选定且可用的财务源是否为 custom。"""
    return _resolve_financial_provider()[0] == "custom"


def _provider_financial_tables(provider: object | None) -> tuple[str, ...]:
    """返回当前 Provider 明确支持的财务子表。"""
    declared = getattr(provider, "financial_tables", FINANCIAL_TABLES)
    declared_set = set(declared)
    return tuple(table for table in FINANCIAL_TABLES if table in declared_set)


def _ensure_financial_table_supported(table: str) -> None:
    pinned = _PINNED_FINANCIAL_PROVIDER.get()
    if not pinned or pinned[0] != "custom":
        return
    if table not in _provider_financial_tables(pinned[1]):
        raise RuntimeError(f"selected financial provider does not support {table}")


def _fetch_table(
    table: str,
    symbols: list[str],
    capset: CapabilitySet,
    latest_only: bool = True,
    strict: bool = False,
) -> pl.DataFrame:
    """通过当前财务数据源拉取一张标准化财务表。"""
    provider_route = _PINNED_FINANCIAL_PROVIDER.get()
    provider_kind, provider = (
        provider_route if provider_route is not None else _resolve_financial_provider()
    )
    if provider_kind == "unavailable":
        logger.info("sync_%s skipped: selected financial provider unavailable", table)
        return pl.DataFrame()
    if provider_kind == "tickflow" and not capset.has(Cap.FINANCIAL):
        logger.info("sync_%s skipped: no FINANCIAL capability", table)
        return pl.DataFrame()
    if not symbols:
        logger.warning("sync_%s skipped: no symbols", table)
        return pl.DataFrame()

    # 自定义数据源分流
    if provider_kind == "custom":
        try:
            assert provider is not None
            df = provider.get_financials(table, symbols, latest_only=latest_only)
        except Exception as e:  # noqa: BLE001
            if strict:
                raise RuntimeError(f"sync_{table} custom provider failed") from e
            logger.warning("sync_%s custom provider failed: %s", table, e)
            return pl.DataFrame()
        if df.is_empty() or "symbol" not in df.columns:
            return pl.DataFrame()
        return df

    from app.tickflow.client import get_client
    tf = get_client()

    # 分批拉取
    api_method = {
        "metrics": tf.financials.metrics,
        "income": tf.financials.income,
        "balance_sheet": tf.financials.balance_sheet,
        "cash_flow": tf.financials.cash_flow,
        "shares": getattr(tf.financials, "shares", None),
    }[table]
    if api_method is None:
        logger.warning("sync_shares skipped: current TickFlow SDK does not support shares")
        return pl.DataFrame()

    all_records: list[dict] = []
    total_batches = (len(symbols) + _BATCH_SIZE - 1) // _BATCH_SIZE

    for i in range(0, len(symbols), _BATCH_SIZE):
        chunk = symbols[i : i + _BATCH_SIZE]
        batch_num = i // _BATCH_SIZE + 1
        try:
            data = api_method(chunk, latest=latest_only)
            # data 格式: { "600519.SH": [record, ...], ... }
            if isinstance(data, dict):
                for sym, records in data.items():
                    if isinstance(records, list):
                        for rec in records:
                            if isinstance(rec, dict):
                                rec["symbol"] = sym
                                all_records.append(rec)
            logger.debug("sync_%s batch %d/%d: %d records", table, batch_num, total_batches, len(data) if isinstance(data, dict) else 0)
        except Exception as e:
            if strict:
                raise RuntimeError(
                    f"sync_{table} batch {batch_num}/{total_batches} failed"
                ) from e
            logger.warning("sync_%s batch %d/%d failed: %s", table, batch_num, total_batches, e)

    if not all_records:
        return pl.DataFrame()

    df = pl.DataFrame(all_records)
    if df.is_empty() or "symbol" not in df.columns:
        return pl.DataFrame()
    return df


@contextmanager
def _financial_commit_guard(data_dir: Path, lease=None, allow_commit=None):
    """Parquet 暂存完成后才锁定路由、取消状态和最终发布。"""
    from contextlib import ExitStack

    from app.services import preferences

    with ExitStack() as stack:
        if lease is not None:
            stack.enter_context(provider_route_commit_guard(
                lease[0], lease[2], preferences.get_financial_provider, "financial",
            ))
            _validate_financial_provider_lease(*lease)
        if allow_commit is not None and not allow_commit():
            raise RuntimeError("financial sync stopped before commit")
        stack.enter_context(financial_snapshot_lock(data_dir))
        yield


def _write_table(table: str, df: pl.DataFrame, data_dir: Path, *, lease=None, allow_commit=None) -> int:
    if df.is_empty() or "symbol" not in df.columns:
        return 0

    # 写入 Parquet (全量覆盖)
    out_dir = data_dir / "financials" / table
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "part.parquet"
    from app.services.atomic_parquet import write_parquet_atomic

    with update_slot("财务", data_dir):
        write_parquet_atomic(
            df, out_file,
            commit_guard=lambda: _financial_commit_guard(data_dir, lease, allow_commit),
        )

    logger.info("sync_%s done: %d records written", table, len(df))
    return len(df)


def _sync_table(
    table: str,
    symbols: list[str],
    data_dir: Path,
    capset: CapabilitySet,
    latest_only: bool = True,
) -> int:
    """同步单张财务表。返回写入的行数。"""
    with update_slot("财务", data_dir), _financial_provider_lease() as lease:
        frame = _fetch_table(table, symbols, capset, latest_only=latest_only)
        return _write_table(table, frame, data_dir, lease=lease)


def _merge_report_history(*frames: pl.DataFrame) -> pl.DataFrame:
    """合并财务报告历史，并保留同报告期的每个公告版本。

    相同公告版本按输入顺序逐列取最后一个非空值；较晚公告缺少的字段仅从同一
    ``(symbol, period_end)`` 的既有较早公告向前填充。这样既能让不同 Provider
    的字段并集共存，也不会折叠修订公告或把未来修订泄漏到更早时点。
    """
    valid = [
        frame
        for frame in frames
        if not frame.is_empty() and {"symbol", "period_end"} <= set(frame.columns)
    ]
    if not valid:
        return pl.DataFrame()
    merged = pl.concat(valid, how="diagonal_relaxed").filter(
        pl.col("symbol").is_not_null() & pl.col("period_end").is_not_null()
    )
    # 同一报告期的原公告与修订公告必须同时保留, 历史回测才能按目标日还原当时
    # 已公开的版本; 仅完全相同的公告版本去重。
    version_columns = ["symbol", "period_end"]
    if "announce_date" in merged.columns:
        version_columns.append("announce_date")

    value_columns = [column for column in merged.columns if column not in version_columns]
    merged = merged.group_by(version_columns, maintain_order=True).agg([
        pl.col(column).drop_nulls().last().alias(column)
        for column in value_columns
    ])
    merged = merged.sort(version_columns, nulls_last=True)
    if "announce_date" in version_columns and value_columns:
        merged = merged.with_columns([
            pl.col(column)
            .forward_fill()
            .over(["symbol", "period_end"])
            .alias(column)
            for column in value_columns
        ])
    return merged


def _sync_history_table_for_symbols(
    table: str,
    symbols: list[str],
    data_dir: Path,
    capset: CapabilitySet,
    commit_guard: Callable[[], bool] | None = None,
) -> int:
    """历史累积同步: 拉取完整公告版本集合并与本地历史合并。

    Provider 暂无按公告日增量接口。为发现较新报告期公布后的旧报告期修订，
    既有标的也必须请求完整历史；写入前按公告版本去重，不覆盖本地独有记录。
    """
    with update_slot("财务", data_dir), _financial_provider_lease() as lease:
        _ensure_financial_table_supported(table)
        existing = get_financial_df(data_dir, table)
        history = _fetch_table(
            table,
            symbols,
            capset,
            latest_only=False,
            strict=True,
        )
        if history.is_empty() or "symbol" not in history.columns:
            raise RuntimeError(f"sync_{table} provider returned no usable rows")
        merged = _merge_report_history(existing, history)
        return _write_table(table, merged, data_dir, lease=lease, allow_commit=commit_guard)


def _prepare_history_table_for_symbols(
    table: str,
    symbols: list[str],
    data_dir: Path,
    capset: CapabilitySet,
) -> pl.DataFrame:
    """在内存中准备完整历史快照，不发布任何文件。"""
    existing = get_financial_df(data_dir, table)
    history = _fetch_table(
        table,
        symbols,
        capset,
        latest_only=False,
        strict=True,
    )
    if history.is_empty() or "symbol" not in history.columns:
        raise RuntimeError(f"sync_{table} provider returned no usable rows")
    return _merge_report_history(existing, history)


def sync_metrics(
    data_dir: Path,
    capset: CapabilitySet,
    commit_guard: Callable[[], bool] | None = None,
) -> int:
    """同步核心财务指标 (metrics), 历史各期累积保留。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols(
        "metrics", symbols, data_dir, capset, commit_guard
    )


def sync_income(
    data_dir: Path,
    capset: CapabilitySet,
    commit_guard: Callable[[], bool] | None = None,
) -> int:
    """同步利润表, 历史各期累积保留。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols(
        "income", symbols, data_dir, capset, commit_guard
    )


def sync_balance_sheet(
    data_dir: Path,
    capset: CapabilitySet,
    commit_guard: Callable[[], bool] | None = None,
) -> int:
    """同步资产负债表, 历史各期累积保留。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols(
        "balance_sheet", symbols, data_dir, capset, commit_guard
    )


def sync_cash_flow(
    data_dir: Path,
    capset: CapabilitySet,
    commit_guard: Callable[[], bool] | None = None,
) -> int:
    """同步现金流量表, 历史各期累积保留。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols(
        "cash_flow", symbols, data_dir, capset, commit_guard
    )


def sync_shares(
    data_dir: Path,
    capset: CapabilitySet,
    commit_guard: Callable[[], bool] | None = None,
) -> int:
    """同步历史股本表。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols(
        "shares", symbols, data_dir, capset, commit_guard
    )


def sync_all(
    data_dir: Path,
    capset: CapabilitySet,
    commit_guard: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """同步所有财务表；五表准备完成后作为一个快照集合发布。"""
    with update_slot("财务", data_dir), _financial_provider_lease() as lease:
        _provider_name, provider_kind, _generation = lease
        if provider_kind == "unavailable":
            logger.info("sync_all financials skipped: selected provider unavailable")
            return {}
        if provider_kind == "tickflow" and not capset.has(Cap.FINANCIAL):
            logger.info("sync_all financials skipped: no FINANCIAL capability")
            return {}

        symbols = _get_symbols(data_dir)
        pinned = _PINNED_FINANCIAL_PROVIDER.get()
        provider = pinned[1] if pinned and pinned[0] == "custom" else None
        supported_tables = _provider_financial_tables(provider)
        snapshots: dict[str, pl.DataFrame] = {}
        for table in supported_tables:
            snapshots[table] = _prepare_history_table_for_symbols(
                table, symbols, data_dir, capset
            )
        results = {table: frame.height for table, frame in snapshots.items()}
        entries = [
            (data_dir / "financials" / table / "part.parquet", frame)
            for table, frame in snapshots.items()
            if not frame.is_empty() and "symbol" in frame.columns
        ]
        replace_parquet_set(
            entries,
            journal_path=_financial_journal_path(data_dir),
            commit_guard=lambda: _financial_commit_guard(data_dir, lease, commit_guard),
        )

    # 同步完成后注册 DuckDB 视图
    _refresh_financials_views(data_dir)

    return results


# ================================================================
# DuckDB 视图
# ================================================================

def _refresh_financials_views(data_dir: Path) -> None:
    """刷新财务表 DuckDB 视图 (在 DataStore.db 上注册)。"""
    d = data_dir.as_posix()
    views = {
        "financials_metrics": f"{d}/financials/metrics/*.parquet",
        "financials_income": f"{d}/financials/income/*.parquet",
        "financials_balance_sheet": f"{d}/financials/balance_sheet/*.parquet",
        "financials_cash_flow": f"{d}/financials/cash_flow/*.parquet",
        "financials_shares": f"{d}/financials/shares/*.parquet",
    }
    for name in views:
        out = data_dir / "financials" / name.replace("financials_", "") / "part.parquet"
        if not out.exists():
            continue
        # 视图注册需要由 DataStore 完成,这里只做日志
        logger.debug("financial parquet ready: %s (%d rows)", name, out.stat().st_size)


def get_financial_df(data_dir: Path, table: str) -> pl.DataFrame:
    """读取本地财务 Parquet；读取前恢复任何未完成的集合发布。"""
    with financial_snapshot_lock(data_dir):
        path = data_dir / "financials" / table / "part.parquet"
        if not path.exists():
            return pl.DataFrame()
        try:
            return pl.read_parquet(path)
        except Exception as e:
            logger.warning("读取 financials/%s 失败: %s", table, e)
            return pl.DataFrame()


def get_financial_snapshot(data_dir: Path) -> dict[str, pl.DataFrame]:
    """在同一集合锁内读取全部财务表，避免观察到发布中间态。"""
    with financial_snapshot_lock(data_dir):
        return {
            table: get_financial_df(data_dir, table)
            for table in FINANCIAL_TABLES
        }


# ================================================================
# 调度器
# ================================================================

class FinancialScheduler:
    """独立调度器: 每周同步 metrics, 财务表支持手动同步。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._data_dir: Path | None = None
        self._capset: CapabilitySet | None = None
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._last_sync: dict[str, str] = {}  # {table: iso_timestamp}
        # 手动同步(run_now)是否正在进行。前端据此显示"同步中"并防重复点击。
        self._is_syncing = False
        self._paused = False
        self._stopping = False
        self._bg_thread: threading.Thread | None = None

    def start(self, data_dir: Path, capset: CapabilitySet, *, auto_schedule: bool = False) -> None:
        """初始化调度器，并按需启动周期同步后台任务。

        auto_schedule=False (默认): 仅初始化 (设置数据目录/能力 + 恢复 last_sync),
            供 /api/financials/sync/* 手动同步使用, 不启动自动调度。
        auto_schedule=True: 额外启动每周一次的 metrics 自动同步 (启动后 60s 首跑)。
        """
        # 先记录 data_dir/capset, 即使当前无 FINANCIAL 也保留引用:
        # 用户稍后在「设置」页升级到 Expert Key 时, update_capabilities() 会把新 capset
        # 推进来,trigger()/run_now() 才能用上 FINANCIAL。否则 _capset 永远是 None,
        # 即便 app.state.capabilities 已更新, 调度器仍报 "no FINANCIAL capability"。
        self._data_dir = data_dir
        self._capset = capset
        with self._lock:
            self._stopping = False
        if not capset.has(Cap.FINANCIAL) and not _financial_is_custom():
            logger.info("FinancialScheduler skipped: no FINANCIAL capability")
            return
        # 从持久化恢复上次同步时间: 重启后前端仍能显示真实最后同步时间,而非"尚未同步"
        try:
            from app.services import preferences
            restored = dict(preferences.get_financial_sync_times())
            # 老用户迁移兜底: 若某表在 preferences 无记录但 parquet 已存在(升级前同步过),
            # 用 parquet 文件的修改时间作为同步时间并补写持久化。
            for table in FINANCIAL_TABLES:
                if table in restored:
                    continue
                parquet = data_dir / "financials" / table / "part.parquet"
                if parquet.exists():
                    mtime = datetime.fromtimestamp(parquet.stat().st_mtime, tz=timezone.utc).isoformat()
                    restored[table] = mtime
                    preferences.set_financial_sync_time(table, mtime)
                    logger.info("FinancialScheduler backfilled last_sync for %s from parquet mtime", table)
            self._last_sync = restored
            if self._last_sync:
                logger.info("FinancialScheduler restored last_sync: %s", list(self._last_sync.keys()))
        except Exception as e:  # noqa: BLE001
            logger.warning("restore financial_sync_times failed: %s", e)

        if not auto_schedule:
            # 仅初始化 (手动同步用), 不启动周期任务。
            logger.info("FinancialScheduler initialized (auto-schedule disabled; manual sync only)")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("FinancialScheduler started (auto-schedule enabled)")

    def _record_sync(self, table: str) -> None:
        """记录一张表的同步完成时间: 更新内存 + 持久化到 preferences.json。

        持久化确保即使重启,前端 /status 仍返回真实的最后同步时间,
        不会错误地显示"尚未同步"。
        """
        ts = datetime.now(timezone.utc).isoformat()
        self._last_sync[table] = ts
        try:
            from app.services import preferences
            preferences.set_financial_sync_time(table, ts)
        except Exception as e:  # noqa: BLE001
            logger.warning("persist financial_sync_time(%s) failed: %s", e)

    def update_capabilities(self, capset: CapabilitySet) -> None:
        """刷新调度器持有的能力集。

        用户在「设置」页新增/清除 API Key 后, settings API 会重新探测能力并更新
        app.state.capabilities; 必须同步推给本调度器, 否则 trigger()/run_now() 仍读
        启动时的旧 capset, 即便 app.state 已含 FINANCIAL, 调度器仍报
        "no FINANCIAL capability" 而拒绝同步 (表现为前端「全部同步」按钮闪一下无动作)。
        """
        prev = self._capset
        self._capset = capset
        had = bool(prev) and prev.has(Cap.FINANCIAL)
        now = capset.has(Cap.FINANCIAL)
        if had != now:
            logger.info(
                "FinancialScheduler capabilities updated: FINANCIAL %s -> %s", had, now
            )

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._stopping = True
            self._paused = True
            task = self._task
            self._task = None
            bg_thread = self._bg_thread
        if task:
            task.cancel()
        if bg_thread is not None and bg_thread is not threading.current_thread():
            bg_thread.join(timeout=10.0)
        logger.info("FinancialScheduler stopped")

    def _commit_allowed(self) -> bool:
        with self._lock:
            return not self._stopping and not self._paused

    async def _run_loop(self) -> None:
        """每周执行一次 metrics 同步。"""
        try:
            while self._running:
                # 首次启动等 60s, 之后每 7 天执行一次
                await asyncio.sleep(60)
                if not self._running:
                    break

                # 每周: 只同步 metrics
                try:
                    result = self.run_now("metrics")
                    logger.info(
                        "FinancialScheduler: metrics sync result=%s", result
                    )
                except Exception as e:
                    logger.warning("FinancialScheduler: metrics sync failed: %s", e)

                # 等待下一次 (7天)
                for _ in range(7 * 24 * 60):  # 每分钟检查一次 _running
                    if not self._running:
                        break
                    await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass

    def _run_body(self, table: str | None) -> dict[str, int]:
        """同步逻辑本体(不加锁,假设调用方已持有 _is_syncing)。

        table=None 同步全部财务表;否则只同步指定表。
        全量同步仅在 Provider 支持的表集合提交后统一更新 last_sync。
        """
        if table:
            fn = {
                "metrics": sync_metrics,
                "income": sync_income,
                "balance_sheet": sync_balance_sheet,
                "cash_flow": sync_cash_flow,
                "shares": sync_shares,
            }.get(table)
            if not fn:
                return {}
            rows = fn(self._data_dir, self._capset, self._commit_allowed)
            self._record_sync(table)
            return {table: rows}
        # 全部同步
        result = sync_all(self._data_dir, self._capset, self._commit_allowed)
        for t in result:
            self._record_sync(t)
        return result

    def run_now(self, table: str | None = None) -> dict[str, int]:
        """同步执行一次同步(阻塞调用线程)。

        ⚠ 全量同步需数分钟,务必在后台线程调用,不要直接在 HTTP 请求线程里阻塞,
        否则请求会长时间 pending 直至被浏览器/代理超时掐断(表现为"点击无反应")。
        HTTP 接口应调用 trigger() 立即返回,再让前端轮询 /status.syncing 看进度。

        用 _is_syncing 标志防并发:若已有同步在进行,本次直接跳过,
        避免重复请求拖慢服务端 / 触发上游限流。
        """
        if not self._capset or (not self._capset.has(Cap.FINANCIAL) and not _financial_is_custom()):
            return {}
        with self._lock:
            if self._paused or self._stopping or self._is_syncing:
                logger.info("financial sync skipped: already running")
                return {"_skipped": 1}
            self._is_syncing = True
        try:
            return self._run_body(table)
        finally:
            with self._lock:
                self._is_syncing = False
                self._idle.notify_all()

    def trigger(self, table: str | None = None) -> dict[str, int]:
        """触发一次同步(非阻塞,立即返回)。

        在后台线程执行同步体,HTTP 请求无需等待。
        返回 {"started": True/False}:
          - False = 能力不足或已有同步在进行(被防并发跳过)
          - True  = 已在后台开始,前端应轮询 /status.syncing 观察进度

        ⚠ _is_syncing 在此处置 True(持锁),确保 trigger 返回时前端轮询
        /status 已能看到 syncing=True,无竞态窗口;同时防止快速重复点击
        启动多个后台线程。后台线程复用 _run_body 执行真正的同步逻辑。
        """
        if not self._capset or (not self._capset.has(Cap.FINANCIAL) and not _financial_is_custom()):
            return {"started": False, "reason": "no FINANCIAL capability"}
        with self._lock:
            if self._paused or self._stopping:
                return {"started": False, "reason": "clearing"}
            if self._is_syncing:
                logger.info("financial sync trigger skipped: already running")
                return {"started": False, "reason": "already running"}
            # 持锁置位:保证 trigger 返回前 syncing 已为 True
            self._is_syncing = True

        def _bg() -> None:
            try:
                self._run_body(table)
            except Exception as e:  # noqa: BLE001
                logger.exception("background financial sync failed: %s", e)
            finally:
                with self._lock:
                    self._is_syncing = False
                    if self._bg_thread is threading.current_thread():
                        self._bg_thread = None
                    self._idle.notify_all()

        t = threading.Thread(target=_bg, name="financial-sync", daemon=True)
        with self._lock:
            if self._stopping or self._paused:
                self._is_syncing = False
                self._idle.notify_all()
                return {"started": False, "reason": "stopping"}
            self._bg_thread = t
        t.start()
        logger.info("financial sync triggered in background: table=%s", table or "all")
        return {"started": True}

    @property
    def is_syncing(self) -> bool:
        """手动同步是否正在进行(供 /status 返回,前端据此显示"同步中")。"""
        with self._lock:
            return self._is_syncing

    @contextmanager
    def quiesced(self):
        """阻止新财务同步并等待在途任务结束，供清库使用。"""
        with self._idle:
            self._paused = True
            while self._is_syncing:
                self._idle.wait()
        try:
            yield
        finally:
            with self._idle:
                self._paused = False
                self._idle.notify_all()

    @property
    def last_sync(self) -> dict[str, str]:
        return dict(self._last_sync)


# 全局单例
financial_scheduler = FinancialScheduler()
