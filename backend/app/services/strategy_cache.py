"""策略结果缓存 — 写入本地文件, 供策略页面秒加载。

缓存结构:
  {
    "as_of": "2024-01-15",
    "results": { strategy_id: { total, as_of, rows } },
    "today_ever_matched": { strategy_id: [symbol, ...] },    // 今日曾命中 symbol 并集
    "today_ever_rows": { strategy_id: { symbol: row_data } },// 今日曾命中的完整行数据
    "updated_at": 1705324800000  # Unix ms
  }

文件路径: data/user_data/strategy_cache.json
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows 开发环境没有 fcntl
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - 非 Windows 环境没有 msvcrt
    msvcrt = None


def _json_default(obj: Any) -> Any:
    """处理 date/datetime 等 JSON 不认识的类型。"""
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


logger = logging.getLogger(__name__)

_CACHE_FILENAME = "strategy_cache.json"
_INVALID_CACHE_SUFFIX = ".invalid"
_GENERATION_SUFFIX = ".generation.json"
_LOCK_SUFFIX = ".lock"
_CACHE_GENERATION_KEY = "_cache_generation"
_STRATEGY_GENERATIONS_KEY = "_strategy_generations"

# 读写同一 JSON 文件的进程内锁: write_cache 的 read-modify-write 与并发 read_cache
# 无锁会丢更新/读到半写文件。read_cache 与 write_cache 共用此锁; write 内部复用
# _read_cache_unlocked 避免自死锁。写入用临时文件 + os.replace 做到原子替换。
_file_lock = threading.Lock()
_invalid_cache_paths: set[Path] = set()
CacheGeneration = tuple[int, dict[str, int]]


class CacheGenerationStateError(RuntimeError):
    """缓存代际状态损坏或无法读取, 必须拒绝缓存读取和回写。"""


def _cache_path(data_dir: Path) -> Path:
    return data_dir / "user_data" / _CACHE_FILENAME


def _invalid_cache_path(path: Path) -> Path:
    return path.with_name(path.name + _INVALID_CACHE_SUFFIX)


def _generation_path(path: Path) -> Path:
    return path.with_name(path.name + _GENERATION_SUFFIX)


@contextmanager
def _process_file_lock(path: Path):
    """为缓存读改写操作加跨进程锁, 避免并发失效覆盖彼此的持久代际。"""
    lock_path = path.with_name(path.name + _LOCK_SUFFIX)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0)
        lock_file.write(b"\\0")
        lock_file.flush()
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows 专用分支
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - 所有受支持平台都应提供一种文件锁
            raise RuntimeError("当前平台不支持策略缓存跨进程锁")
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows 专用分支
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _read_generation_state(path: Path) -> CacheGeneration:
    """读取跨进程缓存代际; 缺失表示旧版本缓存的初始代际。"""
    try:
        payload = json.loads(_generation_path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0, {}
    except Exception as e:
        logger.warning("读取策略缓存代际失败: %s", e)
        raise CacheGenerationStateError("策略缓存代际无法读取") from e

    if not isinstance(payload, dict):
        logger.warning("策略缓存代际格式无效")
        raise CacheGenerationStateError("策略缓存代际格式无效")
    full_generation = payload.get("full_generation", 0)
    strategy_generations = payload.get("strategy_generations", {})
    if not isinstance(full_generation, int) or full_generation < 0:
        logger.warning("策略缓存代际格式无效")
        raise CacheGenerationStateError("策略缓存代际格式无效")
    if not isinstance(strategy_generations, dict):
        logger.warning("策略缓存策略代际格式无效")
        raise CacheGenerationStateError("策略缓存策略代际格式无效")
    if any(
        not isinstance(strategy_id, str)
        or not isinstance(generation, int)
        or generation < 0
        for strategy_id, generation in strategy_generations.items()
    ):
        logger.warning("策略缓存策略代际格式无效")
        raise CacheGenerationStateError("策略缓存策略代际格式无效")
    return full_generation, {
        strategy_id: generation
        for strategy_id, generation in strategy_generations.items()
    }


def _write_generation_state(
    path: Path, full_generation: int, strategy_generations: dict[str, int],
) -> None:
    """原子持久化缓存代际, 使其他进程的旧任务无法重新发布结果。"""
    generation_path = _generation_path(path)
    generation_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = generation_path.with_name(generation_path.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "full_generation": full_generation,
                "strategy_generations": strategy_generations,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, generation_path)


def _enriched_parquet_path(data_dir: Path, as_of: str) -> Path:
    """返回 enriched parquet 文件路径。"""
    return data_dir / "kline_daily_enriched" / f"date={as_of}" / "part.parquet"


def _get_enriched_mtime(data_dir: Path, as_of: str) -> float | None:
    """返回 enriched parquet 文件的 mtime (秒)。文件不存在返回 None。"""
    p = _enriched_parquet_path(data_dir, as_of)
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return None


def read_cache(data_dir: Path) -> dict | None:
    """读取策略缓存文件。返回 None 表示无缓存或读取失败。

    说明: 原先有 enriched mtime 过期校验 (数据文件变化 → 判过期返回 None),
    但在有实时行情的系统里, enriched parquet 每轮被刷新 → mtime 必然变化 →
    缓存被永久判死, 策略页读不到数据。且判过期后不触发重算, 只能让用户手动重跑,
    保护价值有限。故移除: 盘后缓存总能读出, 实时新鲜度由 /api/screener/cached
    端点叠加监控引擎的内存实时结果 (latest_strategy_results) 来保证。
    """
    path = _cache_path(data_dir)
    with _file_lock, _process_file_lock(path):
        try:
            return _read_cache_unlocked(data_dir)
        except CacheGenerationStateError as e:
            logger.warning("策略缓存代际不可用, 拒绝读取缓存: %s", e)
            return None


def cache_generation(data_dir: Path, strategy_ids: Iterable[str]) -> CacheGeneration:
    """返回全量与指定策略的缓存代际, 供异步策略运行在回写前校验。"""
    path = _cache_path(data_dir)
    with _file_lock, _process_file_lock(path):
        full_generation, versions = _read_generation_state(path)
        return full_generation, {
            strategy_id: versions.get(strategy_id, 0)
            for strategy_id in strategy_ids
        }


def clear_cache(data_dir: Path) -> None:
    """删除策略结果缓存并推进代际, 阻止进行中的旧策略运行回写。"""
    path = _cache_path(data_dir)
    with _file_lock, _process_file_lock(path):
        try:
            full_generation, _ = _read_generation_state(path)
            next_generation = full_generation + 1
            _write_generation_state(path, next_generation, {})
            _invalidate_and_remove_cache_files(path)
        except Exception:
            _invalidate_and_remove_cache_files(path)
            raise


def clear_strategy_results(data_dir: Path, strategy_ids: set[str]) -> None:
    """仅删除指定策略的缓存结果, 同时保留未受配置变更影响的策略。"""
    if not strategy_ids:
        return

    path = _cache_path(data_dir)
    with _file_lock, _process_file_lock(path):
        try:
            # 即使当前文件内没有目标策略, 也要推进该策略的代际, 拒绝配置变更前已经开始的回写。
            full_generation, versions = _read_generation_state(path)
            versions = dict(versions)
            for strategy_id in strategy_ids:
                versions[strategy_id] = versions.get(strategy_id, 0) + 1
            _write_generation_state(path, full_generation, versions)
            cached = _read_cache_unlocked(data_dir)
            if not cached:
                return

            results = dict(cached.get("results") or {})
            removed = False
            for strategy_id in strategy_ids:
                if results.pop(strategy_id, None) is not None:
                    removed = True
            if not removed:
                return
            if not results:
                _invalidate_and_remove_cache_files(path)
                return

            payload = dict(cached)
            payload["results"] = results
            for key in ("today_ever_matched", "today_ever_rows"):
                values = dict(cached.get(key) or {})
                for strategy_id in strategy_ids:
                    values.pop(strategy_id, None)
                payload[key] = values
            payload["updated_at"] = int(time.time() * 1000)
            payload[_CACHE_GENERATION_KEY] = full_generation
            payload[_STRATEGY_GENERATIONS_KEY] = {
                strategy_id: versions.get(strategy_id, 0)
                for strategy_id in results
            }

            try:
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default), encoding="utf-8")
                os.replace(tmp, path)
            except Exception as e:
                logger.warning("按策略清理策略缓存失败: %s", e)
                _invalidate_and_remove_cache_files(path)
                raise
        except Exception:
            _invalidate_and_remove_cache_files(path)
            raise


def _read_cache_unlocked(data_dir: Path) -> dict | None:
    """实际读取逻辑 (不持锁)。供 read_cache 与 write_cache 复用, 避免重入死锁。"""
    path = _cache_path(data_dir)
    if path in _invalid_cache_paths or _invalid_cache_path(path).exists():
        return None
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        cached = json.loads(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取策略缓存失败: %s", e)
        return None

    if not isinstance(cached, dict):
        return None

    current_full_generation, current_strategy_generations = _read_generation_state(path)
    cached_full_generation = cached.pop(_CACHE_GENERATION_KEY, 0)
    cached_strategy_generations = cached.pop(_STRATEGY_GENERATIONS_KEY, {})
    if not isinstance(cached_full_generation, int) or cached_full_generation < 0:
        raise CacheGenerationStateError("缓存快照代际格式无效")
    if not isinstance(cached_strategy_generations, dict) or any(
        not isinstance(strategy_id, str)
        or not isinstance(generation, int)
        or generation < 0
        for strategy_id, generation in cached_strategy_generations.items()
    ):
        raise CacheGenerationStateError("缓存快照策略代际格式无效")
    if cached_full_generation != current_full_generation:
        return None

    results = cached.get("results")
    if not isinstance(results, dict):
        return None
    valid_strategy_ids = {
        strategy_id
        for strategy_id in results
        if cached_strategy_generations.get(strategy_id, 0)
        == current_strategy_generations.get(strategy_id, 0)
    }
    if results and not valid_strategy_ids:
        return None
    if len(valid_strategy_ids) == len(results):
        return cached

    filtered = dict(cached)
    filtered["results"] = {
        strategy_id: result
        for strategy_id, result in results.items()
        if strategy_id in valid_strategy_ids
    }
    for key in ("today_ever_matched", "today_ever_rows"):
        values = cached.get(key)
        if isinstance(values, dict):
            filtered[key] = {
                strategy_id: value
                for strategy_id, value in values.items()
                if strategy_id in valid_strategy_ids
            }
    return filtered


def _invalidate_and_remove_cache_files(path: Path) -> None:
    """使缓存立即不可读, 并尽力删除旧文件而不掩盖原始写入错误。"""
    _invalid_cache_paths.add(path)
    try:
        _invalid_cache_path(path).write_text("", encoding="utf-8")
    except OSError as e:
        logger.warning("写入策略缓存失效标记失败: %s", e)
    for candidate in (path, path.with_name(path.name + ".tmp")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("删除失效策略缓存失败: %s", e)


def _clear_invalid_cache_marker(path: Path) -> None:
    """仅在新缓存已原子替换后移除跨进程失效标记。"""
    try:
        _invalid_cache_path(path).unlink(missing_ok=True)
    except OSError as e:
        logger.warning("删除策略缓存失效标记失败: %s", e)
        raise
    _invalid_cache_paths.discard(path)


def _rows_to_symbol_map(rows: list[dict]) -> dict[str, dict]:
    """将 rows 列表转为 {symbol: row_data} 映射。"""
    result: dict[str, dict] = {}
    for row in rows:
        sym = row.get("symbol")
        if sym:
            result[sym] = row
    return result


def write_cache(
    data_dir: Path,
    as_of: str,
    results: dict[str, Any],
    *,
    preserve_newer: bool = False,
    latest_available_as_of: str | date | Callable[[], str | date | None] | None = None,
    only_latest_available: bool = False,
    expected_generation: CacheGeneration | int | None = None,
) -> None:
    """将策略结果写入缓存文件, 同时更新今日曾命中集合。

    - 日期变更时重置 today_ever_matched 和 today_ever_rows
    - 同一天内合并 (并集) 之前曾命中的 symbol, 并用最新行数据更新
    - 设置 preserve_newer 时, 防止历史日期覆盖较新的共享快照
    - latest_available_as_of 可为日期或延迟读取函数. 函数在写锁内调用, 避免
      过期日期快照放行较早任务覆盖并发完成的新结果
    - only_latest_available 仅在能确认且日期等于最新可用交易日时保存; 异常未来
      日期缓存仍可被正常最新交易日替换
    - expected_generation 用于拒绝全量重载前的旧回写, 并跳过配置已失效的策略结果
    """
    path = _cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 整个 read-modify-write 持锁: 避免并发 write 丢更新, 也避免与 read_cache 撕裂
    with _file_lock, _process_file_lock(path):
        try:
            _write_cache_locked(
                path,
                data_dir,
                as_of,
                results,
                preserve_newer,
                latest_available_as_of,
                only_latest_available,
                expected_generation,
            )
        except Exception:
            _invalidate_and_remove_cache_files(path)
            raise


def _write_cache_locked(
    path: Path,
    data_dir: Path,
    as_of: str,
    results: dict[str, Any],
    preserve_newer: bool,
    latest_available_as_of: str | date | Callable[[], str | date | None] | None,
    only_latest_available: bool,
    expected_generation: CacheGeneration | int | None,
) -> None:
    """持 _file_lock 后的实际写入逻辑 (read-merge-write + 原子替换)。"""
    current_full_generation, current_strategy_generations = _read_generation_state(path)
    if expected_generation is not None:
        if isinstance(expected_generation, int):
            if expected_generation != current_full_generation:
                return
        else:
            full_generation, strategy_generations = expected_generation
            if full_generation != current_full_generation:
                return
            results = {
                strategy_id: result
                for strategy_id, result in results.items()
                if strategy_generations.get(strategy_id) == current_strategy_generations.get(strategy_id, 0)
            }
            if not results:
                return
    elif _invalid_cache_path(path).exists():
        # tombstone 只能由在失效后重新捕获代际的任务清除, 避免旧进程重新发布。
        return
    # 读取旧缓存 (已持锁, 走不重入的 _read_cache_unlocked)
    old = _read_cache_unlocked(data_dir)
    old_as_of = old.get("as_of") if old else None

    try:
        incoming_date = date.fromisoformat(as_of)
    except (TypeError, ValueError):
        if only_latest_available:
            return
        incoming_date = None

    try:
        latest_available = (
            latest_available_as_of()
            if callable(latest_available_as_of) else latest_available_as_of
        )
        latest_available_date = (
            latest_available
            if isinstance(latest_available, date)
            else date.fromisoformat(latest_available)
            if latest_available else None
        )
    except Exception as e:
        if only_latest_available:
            logger.warning("无法确认最新可用交易日, 拒绝写入策略缓存: %s", e)
            return
        latest_available_date = None

    if only_latest_available and (incoming_date is None or incoming_date != latest_available_date):
        return

    if preserve_newer and old_as_of and incoming_date is not None:
        try:
            old_date = date.fromisoformat(old_as_of)
            if old_date > incoming_date and (
                latest_available_date is None or old_date <= latest_available_date
            ):
                return
        except (TypeError, ValueError):
            # 旧缓存日期无效时允许新运行修复它。
            pass
    old_ever_rows: dict[str, dict[str, dict]] = old.get("today_ever_rows", {}) if old else {}

    if old_as_of == as_of:
        merged_results = {**(old.get("results") or {}), **results}
    else:
        merged_results = results

    # 当前命中的行数据 → symbol 映射
    current_row_maps: dict[str, dict[str, dict]] = {}
    for sid, r in results.items():
        current_row_maps[sid] = _rows_to_symbol_map(r.get("rows", []))

    if old_as_of and old_as_of == as_of and old_ever_rows:
        # 同一天: 合并 — 用当前行数据更新旧数据 (保持最新价格等)
        merged_rows: dict[str, dict[str, dict]] = {}
        all_keys = set(old_ever_rows.keys()) | set(current_row_maps.keys())
        for sid in all_keys:
            old_map = old_ever_rows.get(sid, {})
            cur_map = current_row_maps.get(sid, {})
            # 以旧数据为基础，用当前数据覆盖 (当前数据更新鲜)
            combined = {**old_map, **cur_map}
            merged_rows[sid] = combined
        today_ever_rows = merged_rows
    else:
        # 新的一天或首次写入
        today_ever_rows = current_row_maps

    # 从 ever_rows 提取 symbol 列表 (用于快速计数)
    today_ever_matched = {sid: sorted(maps.keys()) for sid, maps in today_ever_rows.items()}

    # enriched_mtime: 盘后缓存写入时记录 (向后兼容旧字段)。read_cache 已不再用它
    # 做过期校验, 实时新鲜度改由 /cached 端点叠加监控引擎内存结果保证。
    enriched_mtime = _get_enriched_mtime(data_dir, as_of)

    payload = {
        "as_of": as_of,
        "results": merged_results,
        "today_ever_matched": today_ever_matched,
        "today_ever_rows": today_ever_rows,
        "enriched_mtime": enriched_mtime,
        "updated_at": int(time.time() * 1000),
        _CACHE_GENERATION_KEY: current_full_generation,
        _STRATEGY_GENERATIONS_KEY: {
            strategy_id: current_strategy_generations.get(strategy_id, 0)
            for strategy_id in merged_results
        },
    }
    tmp = path.with_name(path.name + ".tmp")
    try:
        # 原子写: 先写临时文件再 os.replace, 避免读侧读到半写的 JSON
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default), encoding="utf-8")
        os.replace(tmp, path)
        _clear_invalid_cache_marker(path)
        total_rows = sum(len(r.get("rows", [])) for r in merged_results.values())
        total_ever = sum(len(v) for v in today_ever_matched.values())
        logger.info("策略缓存已写入: %s, %d 策略, %d 命中, %d 曾命中", as_of, len(merged_results), total_rows, total_ever)
    except Exception as e:
        logger.warning("写入策略缓存失败: %s", e)
        _invalidate_and_remove_cache_files(path)
        raise
