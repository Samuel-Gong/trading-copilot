"""回归测试: 实时行情模式按数据源和 TickFlow 档位判定。"""
from app.services.quote_service import QuoteService


def test_custom_realtime_source_is_full_market(monkeypatch):
    """自定义实时源(如 fuyao)无视 TickFlow 档位, 恒为全市场。"""
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "fuyao")
    monkeypatch.setattr(QuoteService, "_current_tier", lambda: "free")
    assert QuoteService.realtime_mode() == "full_market"


def test_tickflow_free_uses_watchlist_realtime(monkeypatch):
    """TickFlow 免费有效 key 按能力契约提供自选前 5 只实时行情。"""
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tickflow")
    monkeypatch.setattr(QuoteService, "_current_tier", lambda: "free")
    assert QuoteService.realtime_mode() == "watchlist"
    assert QuoteService.is_realtime_allowed() is True


def test_tickflow_paid_is_full_market(monkeypatch):
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tickflow")
    monkeypatch.setattr(QuoteService, "_current_tier", lambda: "pro")
    assert QuoteService.realtime_mode() == "full_market"


def test_realtime_watchlist_symbols_follow_first_five_entries(monkeypatch):
    """自选实时名单去重并严格限制为自选页前 5 只。"""
    from app.services import preferences, watchlist

    monkeypatch.setattr(watchlist, "list_symbols", lambda: [
        {"symbol": "600000.sh"},
        {"symbol": "600001.SH"},
        {"symbol": "600000.SH"},
        {"symbol": "510300.SH"},
        {"symbol": "000001.SZ"},
        {"symbol": "000002.SZ"},
        {"symbol": "000003.SZ"},
    ])

    assert preferences.get_realtime_watchlist_symbols() == [
        "600000.SH", "600001.SH", "510300.SH", "000001.SZ", "000002.SZ",
    ]
