"""能力重新探测的运行时状态同步测试。"""

from types import SimpleNamespace

from app.api import routes


class _CapabilitySet:
    def to_dict(self) -> dict:
        return {"quote_realtime": True}


def test_redetect_updates_runtime_capabilities_before_restarting_quotes(monkeypatch):
    capset = _CapabilitySet()
    events: list[str] = []
    state = SimpleNamespace(capabilities=object())

    class FinancialScheduler:
        def update_capabilities(self, current) -> None:
            assert current is capset
            assert state.capabilities is capset
            events.append("scheduler")

    class QuoteService:
        def boot_check(self) -> None:
            assert state.capabilities is capset
            events.append("quotes")

    state.financial_scheduler = FinancialScheduler()
    state.quote_service = QuoteService()
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    monkeypatch.setattr(routes, "detect_capabilities", lambda force: capset)
    monkeypatch.setattr(routes, "tier_label", lambda: "Starter")

    result = routes.redetect(request)

    assert result == {
        "label": "Starter",
        "capabilities": {"quote_realtime": True},
    }
    assert events == ["scheduler", "quotes"]
