from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.services import financial_analyzer


def test_current_financial_analysis_uses_latest_version_per_report_period(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = pl.DataFrame(
        {
            "symbol": ["600000.SH"] * 6 + ["000001.SZ"],
            "period_end": [
                date(2025, 12, 31),
                date(2025, 12, 31),
                date(2025, 9, 30),
                date(2025, 6, 30),
                date(2025, 3, 31),
                date(2024, 12, 31),
                date(2025, 12, 31),
            ],
            "announce_date": [
                date(2026, 1, 20),
                date(2026, 2, 10),
                date(2025, 10, 20),
                date(2025, 7, 20),
                date(2025, 4, 20),
                date(2025, 1, 20),
                date(2026, 1, 30),
            ],
            "revenue": [100.0, 110.0, 90.0, 80.0, 70.0, 60.0, 999.0],
        }
    )

    monkeypatch.setattr(
        financial_analyzer,
        "get_financial_df",
        lambda _data_dir, table: metrics if table == "metrics" else pl.DataFrame(),
    )

    rows = financial_analyzer._load_stock_financials(tmp_path, "600000.SH")["metrics"]

    assert [row["period_end"] for row in rows] == [
        date(2025, 12, 31),
        date(2025, 9, 30),
        date(2025, 6, 30),
        date(2025, 3, 31),
    ]
    assert rows[0]["announce_date"] == date(2026, 2, 10)
    assert rows[0]["revenue"] == 110.0
