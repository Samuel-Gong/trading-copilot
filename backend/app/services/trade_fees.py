"""实盘记账的交易费用估算。

按用户费率配置估算佣金与印花税,口径为近似值:
- 佣金 = max(成交额 * 佣金率, 最低佣金),买卖双边,股票与 ETF 同口径
- 印花税 = 成交额 * 印花税率,仅股票卖出收
- 沪市股票过户费 = 成交额 * 过户费率(买卖双边),归入费用(fee)

估算只用于记账方便,实际费用以券商交割单为准并可经导入校准。
"""
from __future__ import annotations

from app.services import preferences


def estimate_trade_cost(
    *,
    asset_type: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    profile: dict | None = None,
) -> tuple[float, float]:
    """返回 (fee, tax)。成交额非正时均为 0。"""
    if profile is None:
        profile = preferences.get_trade_fee_profile()
    turnover = float(quantity) * float(price)
    if turnover <= 0:
        return 0.0, 0.0
    commission = turnover * float(profile["commission_rate"])
    minimum = float(profile["min_commission"])
    if minimum > 0:
        commission = max(commission, minimum)
    fee = commission
    tax = 0.0
    if asset_type != "etf":
        if str(symbol).upper().endswith(".SH"):
            fee += turnover * float(profile["transfer_fee_rate"])
        if side == "sell":
            tax = turnover * float(profile["stamp_tax_rate"])
    return round(fee, 2), round(tax, 2)
