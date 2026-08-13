"""实盘记账费用估算口径与费率配置持久化。"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import preferences, trade_fees


def make_profile(**overrides) -> dict:
    profile = {
        "commission_rate": 0.00025,
        "min_commission": 5.0,
        "stamp_tax_rate": 0.0005,
        "transfer_fee_rate": 0.00001,
    }
    profile.update(overrides)
    return profile


def estimate(asset_type="stock", symbol="600519.SH", side="buy", quantity=100, price=1000, **overrides):
    return trade_fees.estimate_trade_cost(
        asset_type=asset_type,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        profile=make_profile(**overrides),
    )


def test_sh_stock_buy_charges_commission_and_transfer_fee():
    fee, tax = estimate(quantity=100, price=1000, side="buy")
    assert fee == pytest.approx(26.0)
    assert tax == 0.0


def test_sh_stock_sell_adds_stamp_tax():
    fee, tax = estimate(quantity=100, price=1000, side="sell")
    assert fee == pytest.approx(26.0)
    assert tax == pytest.approx(50.0)


def test_sz_stock_has_no_transfer_fee():
    fee, tax = estimate(symbol="000001.SZ", side="sell")
    assert fee == pytest.approx(25.0)
    assert tax == pytest.approx(50.0)


def test_etf_only_charges_commission_even_on_shanghai():
    fee, tax = estimate(asset_type="etf", symbol="510300.SH", side="sell")
    assert fee == pytest.approx(25.0)
    assert tax == 0.0


def test_min_commission_applies_on_small_turnover():
    fee, tax = estimate(quantity=100, price=10, side="buy")
    assert fee == pytest.approx(5.01)
    assert tax == 0.0


def test_zero_min_commission_disables_floor():
    fee, _tax = estimate(quantity=100, price=10, side="buy", min_commission=0)
    assert fee == pytest.approx(0.26)


def test_non_positive_turnover_is_free():
    assert estimate(quantity=0, price=10) == (0.0, 0.0)


def test_fee_profile_defaults_and_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    default = preferences.get_trade_fee_profile()
    assert default["commission_rate"] == pytest.approx(0.00025)
    assert default["min_commission"] == pytest.approx(5.0)

    saved = preferences.set_trade_fee_profile({"min_commission": 0})
    assert saved["min_commission"] == 0.0
    assert saved["stamp_tax_rate"] == pytest.approx(0.0005)

    (tmp_path / "user_data" / "preferences.json").write_text(
        '{"trade_fee_profile": {"commission_rate": -1, "stamp_tax_rate": 0.001}}',
        encoding="utf-8",
    )
    merged = preferences.get_trade_fee_profile()
    assert merged["commission_rate"] == pytest.approx(0.00025)
    assert merged["stamp_tax_rate"] == pytest.approx(0.001)
