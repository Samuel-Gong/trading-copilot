"""扩展数据定时拉取窗口测试。"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services import ext_pull
from app.services.ext_data import ExtConfig, PullConfig


@pytest.mark.asyncio
async def test_scheduler_waits_until_window_start_after_skip(monkeypatch, tmp_path) -> None:
    """长周期任务在窗口外启动时, 应等待到当天窗口, 不能错过整天。"""
    config = ExtConfig(
        id="test_pull",
        label="测试拉取",
        mode="snapshot",
        fields=[],
        pull=PullConfig(
            url="https://example.invalid/data",
            enabled=True,
            schedule_minutes=1440,
            time_window_start="15:00",
            time_window_end="16:00",
        ),
    )

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return cls(2026, 8, 24, 2, 0, 0, tzinfo=UTC).astimezone(tz)
            return cls(2026, 8, 24, 10, 0, 0)

    class _Store:
        def __init__(self, _data_dir) -> None:
            pass

        def get(self, config_id: str):
            return config if config_id == config.id else None

        def upsert(self, _config) -> None:
            pass

    scheduler = ext_pull.PullScheduler()
    scheduler._running = True
    scheduler._data_dir = tmp_path
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        scheduler._running = False

    monkeypatch.setattr(ext_pull, "datetime", _FixedDateTime)
    monkeypatch.setattr(ext_pull, "ExtConfigStore", _Store)
    monkeypatch.setattr(ext_pull.asyncio, "sleep", _sleep)

    await scheduler._run_loop(config)

    assert sleeps == [5 * 60 * 60]
    assert config.pull.next_run == "2026-08-24T07:00:00+00:00"


def test_seconds_until_window_start_handles_cross_midnight_and_invalid_value() -> None:
    now = datetime(2026, 8, 24, 3, 0, 0)
    assert ext_pull._seconds_until_window_start("22:00", now=now) == 19 * 60 * 60
    assert ext_pull._seconds_until_window_start("invalid", now=now) is None


def test_pull_window_uses_beijing_time_for_aware_clock() -> None:
    """部署主机为 UTC 时，配置窗口仍须按北京时间判断和等待。"""
    now = datetime(2026, 8, 24, 7, 30, 0, tzinfo=UTC)

    assert ext_pull._in_time_window("15:00", "16:00", now=now) is True
    assert ext_pull._seconds_until_window_start("16:00", now=now) == 30 * 60
