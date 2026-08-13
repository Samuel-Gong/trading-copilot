"""每日复盘公开 HTTP 快乐路径契约。"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta
from types import SimpleNamespace
from typing import ClassVar

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.daily_review import router as daily_review_router
from app.api.portfolio import router as portfolio_router
from app.config import settings
from app.services import daily_analysis_graph, daily_review, market_recap_reports, stock_reports
from app.services.stock_analyzer import StockAnalysisInput


class FakeDailyReviewRepo:
    names: ClassVar[dict[str, str]] = {
        "600519.SH": "贵州茅台",
        "000001.SZ": "平安银行",
        "300001.SZ": "策略样本",
    }
    prices: ClassVar[dict[str, float]] = {
        "600519.SH": 1600.0,
        "000001.SZ": 12.0,
        "300001.SZ": 20.0,
    }

    def get_name_map(self, symbols=None):
        if symbols is None:
            return dict(self.names)
        return {symbol: self.names[symbol] for symbol in symbols if symbol in self.names}

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        if symbol not in self.prices:
            return pl.DataFrame()
        close = self.prices[symbol]
        dates = getattr(self, "dates_by_symbol", {}).get(
            symbol,
            [date(2026, 8, 1) - timedelta(days=offset) for offset in range(29, -1, -1)],
        )
        return pl.DataFrame({
            "date": dates,
            "open": [close - 0.2] * len(dates),
            "high": [close + 0.5] * len(dates),
            "low": [close - 0.5] * len(dates),
            "close": [close] * len(dates),
            "volume": [100_000] * len(dates),
        })

    def get_enriched_latest_asset(self, asset_type):
        return pl.DataFrame({
            "date": [date(2026, 8, 1)],
            "symbol": ["300001.SZ"],
            "name": ["策略样本"],
            "close": [20.0],
        }), date(2026, 8, 1)

    def get_instruments_asset(self, asset_type):
        return pl.DataFrame({
            "symbol": ["300001.SZ"],
            "name": ["策略样本"],
        })


class FakeStrategyEngine:
    def list_strategies(self):
        return [{
            "id": "test_strategy",
            "name": "测试策略",
            "asset_types": ["stock"],
            "timeframes": ["1d"],
        }]

    def required_history_bars(self, strategy_ids, **kwargs):
        return 1

    def run_all(self, context, **kwargs):
        return {
            "test_strategy": SimpleNamespace(
                rows=[{"symbol": "300001.SZ", "name": "策略样本", "score": 88.0}],
                scores={"300001.SZ": 88.0},
            )
        }


def fake_structured_agent_output(role: str, symbol: str, call_number: int) -> str:
    """生成通过生产 Pydantic 契约的模型边界假数据。"""
    trace = f"{symbol} 的 {role} 第 {call_number} 次可追溯客观结论"
    payloads = {
        "bull_researcher": {
            "supporting_viewpoints": [f"{trace},积极指标仍有研究价值"],
            "bear_response": [f"{trace},已回应上一轮反证"],
            "evidence_chain": ["冻结事实与上游分析共同支持该判断"],
            "conditions": ["后续事实继续满足当前证据前提"],
        },
        "bear_researcher": {
            "counter_evidence": [f"{trace},现有材料仍暴露竞争弱点"],
            "bull_response": [f"{trace},已质询上一轮支持性观点"],
            "fragile_assumptions": ["积极叙事依赖尚待验证的持续性"],
            "invalidation_conditions": ["新增事实无法继续支持积极假设"],
        },
        "research_manager": {
            "confirmed_consensus": [f"{trace},双方均认可已有冻结事实"],
            "unresolved_disagreements": ["指标持续性的解释仍有分歧"],
            "evidence_assessment": [
                {"conclusion": "现有事实可追溯", "grade": "高", "basis": "冻结输入"}
            ],
            "research_ruling": "支持性材料略占优势,但仍需观察关键条件",
            "research_status": "支持性证据占优",
        },
        "trader": {
            "research_status": "支持性证据占优",
            "observation_items": [f"{trace},继续观察关键指标"],
            "upgrade_or_invalidation_facts": ["等待能够验证指标持续性的新事实"],
        },
        "aggressive_risk": {
            "support_factors": [f"{trace},积极因素仍可继续研究"],
            "required_conditions": ["关键指标保持连续且可追溯"],
            "amplified_risks": ["高不确定性可能放大结论偏差"],
        },
        "conservative_risk": {
            "tail_risks": [f"{trace},尾部风险仍需单独跟踪"],
            "weakest_evidence": ["部分结论仅有单一来源支持"],
            "unacceptable_data_gaps": ["缺少验证关键假设的连续数据"],
        },
        "neutral_risk": {
            "common_risks": [f"{trace},双方都承认事实完整性风险"],
            "disagreement_risks": ["双方对指标持续性的权重不同"],
            "minimum_observation_set": ["补充连续指标和相关事件事实"],
        },
        "portfolio_manager": {
            "overall_conclusion": f"{trace},研究流程已经完成",
            "evidence_consensus": ["冻结输入中的核心事实已得到共同确认"],
            "key_disagreements": ["积极指标能否持续仍有分歧"],
            "risk_boundary": ["数据不完整时不得提高结论置信度"],
            "facts_to_watch": ["后续连续指标与相关事件事实"],
            "research_status": "支持性证据占优",
        },
    }
    return json.dumps(payloads[role], ensure_ascii=False)


def make_client(tmp_path, monkeypatch, calls: dict) -> TestClient:
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    async def fake_recap_market_once(*args, **kwargs):
        calls["market"] += 1
        calls["market_news"] = list(kwargs.get("news") or [])
        return "# 2026-08-01 大盘复盘\n\n市场保持震荡。", {
            "as_of": "2026-08-01",
            "summary": "市场保持震荡",
            "emotion_score": 55,
            "emotion_label": "震荡",
        }

    async def fake_collect_review_news(data_dir, as_of):
        calls["news"] = calls.get("news", 0) + 1
        items = list(calls.get("news_items") or [])
        from app.services import review_news

        if items:
            review_news.store_items(data_dir, items)
        return {
            "status": "completed",
            "source_status": "completed",
            "as_of": as_of.isoformat(),
            "cutoff_at": f"{as_of.isoformat()}T23:59:59+08:00",
            "unknown_timestamp_policy": "excluded",
            "items": review_news.query_items(data_dir, as_of=as_of),
            "item_count": len(review_news.query_items(data_dir, as_of=as_of)),
            "errors": [],
        }

    async def fake_generate_ai_text(messages, **kwargs):
        system_prompt = messages[0]["content"]
        user_prompt = messages[-1]["content"]
        symbol_match = re.search(r"标的标准代码: ([A-Z0-9.]+)", user_prompt)
        if symbol_match is None:
            symbol_match = re.search(r"([0-9]{6}\.(?:SH|SZ))", user_prompt)
        symbol = symbol_match.group(1) if symbol_match else "UNKNOWN"
        source_match = re.search(r'"source_ref": "([^"]+)"', user_prompt)
        source_ref = source_match.group(1) if source_match else symbol
        role_match = re.search(r"角色标识: ([a-z_]+)", system_prompt)
        role = role_match.group(1) if role_match else "unknown"
        calls.setdefault("agent_calls", []).append((source_ref, role))
        role_call_number = calls["agent_calls"].count((source_ref, role))
        calls.setdefault("agent_prompts", []).append(
            {
                "source_ref": source_ref,
                "role": role,
                "call_number": role_call_number,
                "user_prompt": user_prompt,
            }
        )
        if role == "market_analyst":
            calls.setdefault("target_prompts", {})[source_ref] = user_prompt
        failure_key = f"{source_ref}:{role}"
        if calls.setdefault("fail_on_call", {}).get(failure_key) == role_call_number:
            raise RuntimeError("模型在指定辩论轮次临时不可用")
        if failure_key in calls.setdefault("fail_once", set()):
            calls["fail_once"].remove(failure_key)
            raise RuntimeError("模型临时不可用")
        raw_output = calls.setdefault("raw_outputs", {}).get(failure_key)
        if raw_output is not None:
            return raw_output
        if role in {
            "bull_researcher",
            "bear_researcher",
            "research_manager",
            "trader",
            "aggressive_risk",
            "conservative_risk",
            "neutral_risk",
            "portfolio_manager",
        }:
            return fake_structured_agent_output(role, symbol, role_call_number)
        return (
            f"### 摘要\n\n{symbol} 的 {role} 第 {role_call_number} 次可追溯客观结论。\n\n"
            "### 证据\n\n仅使用冻结输入和上游材料。"
        )

    monkeypatch.setattr("app.services.daily_review.recap_market_once", fake_recap_market_once)
    monkeypatch.setattr(
        "app.services.review_news.collect_review_news", fake_collect_review_news
    )
    monkeypatch.setattr(
        "app.services.daily_analysis_graph.generate_ai_text", fake_generate_ai_text
    )

    app = FastAPI()
    app.state.repo = FakeDailyReviewRepo()
    app.state.quote_service = None
    app.state.depth_service = None
    app.state.strategy_engine = FakeStrategyEngine()
    app.include_router(portfolio_router)
    app.include_router(daily_review_router)
    return TestClient(app)


def create_account(client: TestClient, name: str = "主账户") -> dict:
    response = client.post("/api/portfolio/accounts", json={"name": name})
    assert response.status_code == 201
    return response.json()


def put_position(
    client: TestClient,
    account_id: str,
    symbol: str,
    quantity: float,
    average_cost: float,
    purchase_date: str | None = None,
) -> dict:
    response = client.post(
        "/api/portfolio/trades",
        json={
            "account_id": account_id,
            "symbol": symbol,
            "trade_date": purchase_date or "2026-07-01",
            "side": "buy",
            "quantity": quantity,
            "price": average_cost,
            # 显式零费用: 防止费率估算改变声明成本, 影响复盘断言的精确数值
            "fee": 0,
            "tax": 0,
        },
    )
    assert response.status_code == 201
    return response.json()


def test_graph_definition_exposes_static_topology_prompts_and_io_contracts(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)

    response = client.get("/api/daily-review/graph-definition")

    assert response.status_code == 200
    definition = response.json()
    assert definition["framework"] == "TradingAgents"
    assert definition["mode"] == "research_only"
    assert definition["execution_enabled"] is False
    research_debate = definition["debates"]["research"]
    assert research_debate == {
        "id": "research",
        "label": "Bull / Bear Research Debate",
        "participants": ["bull_researcher", "bear_researcher"],
        "speaker_order": ["bull_researcher", "bear_researcher"],
        "max_rounds": 2,
        "max_turns": 4,
        "stop_condition": "完成 2 轮、共 4 次交替发言后进入 Research Manager",
    }
    assert len(definition["nodes"]) == 13
    assert len(definition["groups"]) == 6
    nodes = {node["id"]: node for node in definition["nodes"]}
    assert "execution" not in nodes
    assert nodes["facts"]["prompt"]["invokes_model"] is False
    assert nodes["market_analyst"]["prompt"]["system"].startswith(
        "角色标识: market_analyst"
    )
    assert "不得输出买入、卖出" in nodes["market_analyst"]["prompt"]["system"]
    assert nodes["market_analyst"]["position"] == {"x": 260, "y": 78}
    bull_inputs = {item["id"] for item in nodes["bull_researcher"]["required_inputs"]}
    bear_inputs = {item["id"] for item in nodes["bear_researcher"]["required_inputs"]}
    assert "research_debate_history" in bull_inputs
    assert bear_inputs == {
        "frozen_facts",
        "market_analyst",
        "sentiment_analyst",
        "news_analyst",
        "fundamentals_analyst",
        "research_debate_history",
    }
    assert "对方上一轮观点" in nodes["bull_researcher"]["prompt"]["user_template"]
    assert "此前全部有效发言" in nodes["bear_researcher"]["prompt"]["user_template"]
    assert "两轮、四次有效发言" in nodes["research_manager"]["prompt"]["user_template"]
    assert [
        item["label"] for item in nodes["portfolio_manager"]["required_outputs"]
    ] == [
        "综合结论",
        "证据共识",
        "关键分歧",
        "风险边界",
        "仍需观察的事实",
        "研究状态",
    ]
    assert any(edge["kind"] == "feedback" for edge in definition["edges"])


def test_graph_definition_exposes_chinese_tradingagents_prompts_and_pydantic_contracts(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)

    response = client.get("/api/daily-review/graph-definition")

    assert response.status_code == 200
    nodes = {node["id"]: node for node in response.json()["nodes"]}
    expected = {
        "bull_researcher": ("支持性研究论证方", "增长潜力", "BullResearchResult"),
        "bear_researcher": ("反方研究论证方", "竞争弱点", "BearResearchResult"),
        "research_manager": ("研究经理兼辩论主持人", "研究状态量表", "ResearchManagerResult"),
        "trader": ("研究方案代理", "后续研究方案", "TraderResearchPlan"),
        "aggressive_risk": ("积极风险视角", "高不确定性", "AggressiveRiskReview"),
        "conservative_risk": ("保守风险视角", "尾部风险", "ConservativeRiskReview"),
        "neutral_risk": ("中性风险视角", "平衡", "NeutralRiskReview"),
        "portfolio_manager": ("研究组合经理", "最终研究结论", "PortfolioResearchDecision"),
    }

    for node_id, (role_text, focus_text, model_name) in expected.items():
        prompt = nodes[node_id]["prompt"]
        assert role_text in prompt["system"]
        assert focus_text in prompt["system"]
        assert "不得输出买入、卖出" in prompt["system"]
        assert "只返回一个符合下方 JSON Schema 的 JSON 对象" in prompt["system"]
        assert prompt["response_format"]["type"] == "pydantic"
        assert prompt["response_format"]["model"] == model_name
        assert prompt["response_format"]["json_schema"]["additionalProperties"] is False

    assert nodes["market_analyst"]["prompt"]["response_format"] is None
    assert nodes["facts"]["prompt"]["response_format"] is None


def test_graph_run_persists_validated_pydantic_data_and_renders_markdown(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)

    response = client.post(
        "/api/daily-review/routines/2026-08-01/run",
        json={"strategy_ids": ["test_strategy"]},
    )

    assert response.status_code == 202
    routine = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]
    graph_nodes = {node["id"]: node for node in routine["positions"][0]["graph"]["nodes"]}
    expected = {
        "bull_researcher": ("BullResearchResult", "### 支持观点"),
        "bear_researcher": ("BearResearchResult", "### 主要反证"),
        "research_manager": ("ResearchManagerResult", "### 研究裁决"),
        "trader": ("TraderResearchPlan", "### 后续观察清单"),
        "aggressive_risk": ("AggressiveRiskReview", "### 可支持因素"),
        "conservative_risk": ("ConservativeRiskReview", "### 尾部风险"),
        "neutral_risk": ("NeutralRiskReview", "### 共同风险"),
        "portfolio_manager": ("PortfolioResearchDecision", "### 综合结论"),
    }

    for node_id, (schema_name, heading) in expected.items():
        output = graph_nodes[node_id]["output"]
        assert graph_nodes[node_id]["status"] == "completed"
        assert output["schema"] == schema_name
        assert isinstance(output["data"], dict)
        assert heading in output["markdown"]


def test_graph_run_rejects_structured_output_that_breaks_research_redline(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)
    source_ref = f"{account['id']}:600519.SH"
    calls["raw_outputs"] = {
        f"{source_ref}:bull_researcher": json.dumps(
            {
                "supporting_viewpoints": ["建议买入以把握增长潜力"],
                "bear_response": ["已回应反方观点"],
                "evidence_chain": ["冻结事实支持判断"],
                "conditions": ["积极指标保持连续"],
            },
            ensure_ascii=False,
        )
    }

    response = client.post(
        "/api/daily-review/routines/2026-08-01/run",
        json={"strategy_ids": ["test_strategy"]},
    )

    assert response.status_code == 202
    routine = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]
    position = routine["positions"][0]
    nodes = {node["id"]: node for node in position["graph"]["nodes"]}
    assert routine["status"] == "degraded"
    assert position["status"] == "failed"
    assert nodes["bull_researcher"]["status"] == "failed"
    assert "研究输出包含被禁止的交易执行内容" in nodes["bull_researcher"]["error"]
    assert nodes["research_manager"]["status"] == "blocked"
    assert nodes["portfolio_manager"]["status"] == "blocked"


def test_graph_runtime_persists_active_data_flow_before_node_completion(
    tmp_path, monkeypatch
):
    graph = daily_analysis_graph.new_graph(
        target_type="candidate",
        target_ref="candidate:600519.SH",
        symbol="600519.SH",
    )
    snapshots: list[dict] = []

    monkeypatch.setattr(
        daily_analysis_graph,
        "build_stock_analysis_input",
        lambda *args, **kwargs: StockAnalysisInput(
            symbol="600519.SH",
            summary="复盘价和关键价位已冻结",
            levels={},
            close=1600.0,
            user_prompt='标的标准代码: 600519.SH\n"source_ref": "candidate:600519.SH"',
        ),
    )

    async def fake_generate(messages, **kwargs):
        role = re.search(r"角色标识: ([a-z_]+)", messages[0]["content"]).group(1)
        if role in {
            "bull_researcher",
            "bear_researcher",
            "research_manager",
            "trader",
            "aggressive_risk",
            "conservative_risk",
            "neutral_risk",
            "portfolio_manager",
        }:
            return fake_structured_agent_output(role, "600519.SH", 1)
        return f"### 摘要\n\n{role} 已完成。"

    monkeypatch.setattr(daily_analysis_graph, "generate_ai_text", fake_generate)

    completed = asyncio.run(
        daily_analysis_graph.run_graph(
            graph,
            repo=object(),
            data_dir=tmp_path,
            target_context={"source_ref": "candidate:600519.SH"},
            as_of=date(2026, 8, 1),
            on_update=snapshots.append,
        )
    )

    active = next(
        snapshot
        for snapshot in snapshots
        if any(
            node["id"] == "market_analyst" and node["status"] == "running"
            for node in snapshot["nodes"]
        )
    )
    active_edge = next(
        edge
        for edge in active["edges"]
        if edge["source"] == "facts" and edge["target"] == "market_analyst"
    )
    assert active_edge["status"] == "active"
    assert "market_analyst" in active["progress"]["active_node_ids"]
    assert active["progress"]["current_stage"] == "Analyst Team"
    bear_turn = next(
        snapshot
        for snapshot in snapshots
        if snapshot["debates"]["research"]["status"] == "running"
        and snapshot["debates"]["research"]["current_round"] == 1
        and snapshot["debates"]["research"]["current_speaker_id"]
        == "bear_researcher"
        and any(
            node["id"] == "bear_researcher" and node["status"] == "running"
            for node in snapshot["nodes"]
        )
    )
    bull_feedback = next(
        snapshot
        for snapshot in snapshots
        if snapshot["debates"]["research"]["status"] == "running"
        and snapshot["debates"]["research"]["current_round"] == 2
        and snapshot["debates"]["research"]["current_speaker_id"]
        == "bull_researcher"
        and any(
            node["id"] == "bull_researcher" and node["status"] == "running"
            for node in snapshot["nodes"]
        )
    )
    assert next(
        edge
        for edge in bear_turn["edges"]
        if edge["source"] == "bull_researcher" and edge["target"] == "bear_researcher"
    )["status"] == "active"
    assert next(
        edge
        for edge in bull_feedback["edges"]
        if edge["source"] == "bear_researcher" and edge["target"] == "bull_researcher"
    )["status"] == "active"
    assert bull_feedback["progress"]["current_stage"] == (
        "Researcher Team · 第 2/2 轮 · Bull Researcher"
    )
    assert completed["status"] == "completed"
    assert completed["progress"]["percent"] == 100


def test_one_click_run_freezes_positions_and_persists_three_readable_chapters(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)
    put_position(client, account["id"], "000001.SZ", 1000, 10)

    accepted = client.post(
        "/api/daily-review/routines/2026-08-01/run",
        json={"strategy_ids": ["test_strategy"]},
    )

    assert accepted.status_code == 202
    assert accepted.json()["business_date"] == "2026-08-01"
    assert len(accepted.json()["positions"]) == 2

    routine = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]
    assert routine["status"] == "completed"
    assert routine["market_review"]["status"] == "completed"
    assert routine["market_review"]["report"]["content"].startswith("# 2026-08-01")
    assert [item["status"] for item in routine["positions"]] == ["completed", "completed"]
    assert all(item["report"]["content"].startswith("# ") for item in routine["positions"])
    assert routine["scope_summary"]["position_count"] == 2
    assert routine["scope_summary"]["total_cost"] == 160000.0
    assert routine["scope_summary"]["market_value"] == 172000.0
    assert routine["scope_summary"]["missing_price_count"] == 0
    assert {item["price_date"] for item in routine["positions"]} == {"2026-08-01"}
    assert calls["market"] == 1
    assert routine["strategy_screening"]["status"] == "completed"
    assert routine["strategy_screening"]["selection_source"] == "screener_pool"
    assert routine["strategy_screening"]["strategy_ids"] == ["test_strategy"]
    assert routine["strategy_screening"]["candidate_count"] == 1
    assert routine["candidates"][0]["symbol"] == "300001.SZ"
    assert routine["candidates"][0]["matched_strategies"][0]["id"] == "test_strategy"
    assert routine["candidates"][0]["graph"]["id"] == "candidate:300001.SZ"
    assert all(
        node["status"] == "completed"
        for item in [*routine["positions"], *routine["candidates"]]
        for node in item["graph"]["nodes"]
    )
    assert len(calls["agent_calls"]) == 42

    graph = routine["positions"][0]["graph"]
    assert graph["schema_version"] == 4
    assert graph["framework"] == "TradingAgents"
    assert graph["mode"] == "research_only"
    assert graph["progress"] == {
        "completed": 13,
        "total": 13,
        "percent": 100,
        "active_node_ids": [],
        "failed_node_ids": [],
        "current_team_id": "decision",
        "current_stage": "Research Decision",
    }
    node_ids = {node["id"] for node in graph["nodes"]}
    assert node_ids == {
        "facts",
        "market_analyst",
        "sentiment_analyst",
        "news_analyst",
        "fundamentals_analyst",
        "bull_researcher",
        "bear_researcher",
        "research_manager",
        "trader",
        "aggressive_risk",
        "conservative_risk",
        "neutral_risk",
        "portfolio_manager",
    }
    assert "execution" not in node_ids
    assert all(set(node["position"]) == {"x", "y"} for node in graph["nodes"])
    assert all(node["input"] and node["output"] for node in graph["nodes"])
    assert all(node["output"]["sections"] for node in graph["nodes"])
    assert all(edge["status"] == "completed" for edge in graph["edges"])
    assert any(edge["kind"] == "feedback" and not edge["dependency"] for edge in graph["edges"])
    assert len(graph["events"]) == 30
    debate = graph["debates"]["research"]
    assert debate["status"] == "completed"
    assert debate["completed_turns"] == 4
    assert debate["current_round"] == 2
    assert debate["current_speaker_id"] is None
    assert [
        (turn["round"], turn["speaker_id"], turn["status"])
        for turn in debate["history"]
    ] == [
        (1, "bull_researcher", "completed"),
        (1, "bear_researcher", "completed"),
        (2, "bull_researcher", "completed"),
        (2, "bear_researcher", "completed"),
    ]
    graph_nodes = {node["id"]: node for node in graph["nodes"]}
    assert graph_nodes["bull_researcher"]["attempt"] == 2
    assert graph_nodes["bear_researcher"]["attempt"] == 2
    assert len(graph_nodes["bull_researcher"]["turns"]) == 2
    assert len(graph_nodes["bear_researcher"]["turns"]) == 2

    source_ref = f"{account['id']}:600519.SH"
    debate_calls = [
        item
        for item in calls["agent_prompts"]
        if item["source_ref"] == source_ref
        and item["role"] in {"bull_researcher", "bear_researcher"}
    ]
    assert [item["role"] for item in debate_calls] == [
        "bull_researcher",
        "bear_researcher",
        "bull_researcher",
        "bear_researcher",
    ]
    assert "bear_researcher 第 1 次" in debate_calls[2]["user_prompt"]
    assert "bull_researcher 第 2 次" in debate_calls[3]["user_prompt"]

    assert '"quantity": 100' in calls["target_prompts"][f"{account['id']}:600519.SH"]
    assert '"average_cost": 10' in calls["target_prompts"][f"{account['id']}:000001.SZ"]


def test_daily_review_values_position_only_with_business_date_close(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    client.app.state.repo.dates_by_symbol = {
        "600519.SH": [date(2026, 7, 31)],
    }
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)

    response = client.post(
        "/api/daily-review/routines/2026-08-01/run",
        json={"strategy_ids": []},
    )

    assert response.status_code == 202
    routine = response.json()
    position = routine["positions"][0]
    assert position["current_price"] is None
    assert position["price_date"] is None
    assert position["price_available"] is False
    assert position["market_value"] is None
    assert position["unrealized_pnl"] is None
    assert routine["scope_summary"]["priced_position_count"] == 0
    assert routine["scope_summary"]["missing_price_count"] == 1
    assert routine["scope_summary"]["market_value"] == 0.0


def test_explicit_empty_screener_pool_freezes_zero_candidates_without_running_all(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)

    def unexpected_run_all(*args, **kwargs):
        raise AssertionError("空策略池不应退化成运行全部策略")

    client.app.state.strategy_engine.run_all = unexpected_run_all

    accepted = client.post(
        "/api/daily-review/routines/2026-08-01/run",
        json={"strategy_ids": []},
    )
    routine = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]

    assert accepted.status_code == 202
    assert routine["status"] == "completed"
    assert routine["strategy_screening"] == {
        "status": "completed",
        "selection_source": "screener_pool",
        "strategy_ids": [],
        "strategies": [],
        "candidate_count": 0,
        "selection_method": "strategy_pool_order_then_result_order_v1",
        "error": None,
    }
    assert routine["candidates"] == []

    missing_body = client.post("/api/daily-review/routines/2026-07-31/run")
    missing_body_routine = client.get(
        "/api/daily-review/routines/2026-07-31"
    ).json()["routine"]
    assert missing_body.status_code == 202
    assert missing_body_routine["status"] == "completed"
    assert missing_body_routine["strategy_screening"]["selection_source"] == "screener_pool"
    assert missing_body_routine["strategy_screening"]["strategy_ids"] == []
    assert missing_body_routine["candidates"] == []


def test_daily_review_runs_only_the_strategy_ids_frozen_from_screener_pool(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": [], "screened_strategy_ids": []}
    client = make_client(tmp_path, monkeypatch, calls)

    class TwoStrategyEngine(FakeStrategyEngine):
        def list_strategies(self):
            return [
                *super().list_strategies(),
                {
                    "id": "other_strategy",
                    "name": "未选策略",
                    "asset_types": ["stock"],
                    "timeframes": ["1d"],
                },
            ]

        def run_all(self, context, **kwargs):
            calls["screened_strategy_ids"] = list(kwargs["strategy_ids"])
            return {
                **super().run_all(context, **kwargs),
                "other_strategy": SimpleNamespace(
                    rows=[{"symbol": "600519.SH", "name": "贵州茅台", "score": 99.0}],
                    scores={"600519.SH": 99.0},
                ),
            }

    client.app.state.strategy_engine = TwoStrategyEngine()

    client.post(
        "/api/daily-review/routines/2026-08-01/run",
        json={"strategy_ids": ["test_strategy"]},
    )
    routine = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]

    assert calls["screened_strategy_ids"] == ["test_strategy"]
    assert routine["strategy_screening"]["selection_source"] == "screener_pool"
    assert routine["strategy_screening"]["strategies"] == [
        {"id": "test_strategy", "name": "测试策略"}
    ]
    assert [item["symbol"] for item in routine["candidates"]] == ["300001.SZ"]
    assert routine["candidates"][0]["matched_strategies"] == [
        {"id": "test_strategy", "name": "测试策略", "rank": 1, "score": 88.0}
    ]


def test_candidate_order_follows_strategy_pool_and_original_result_order(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)

    class OrderedStrategyEngine(FakeStrategyEngine):
        def list_strategies(self):
            return [
                *super().list_strategies(),
                {
                    "id": "second_strategy",
                    "name": "第二策略",
                    "asset_types": ["stock"],
                    "timeframes": ["1d"],
                },
            ]

        def run_all(self, context, **kwargs):
            assert kwargs["strategy_ids"] == ["test_strategy", "second_strategy"]
            return {
                "test_strategy": SimpleNamespace(
                    rows=[
                        {"symbol": "300001.SZ", "name": "策略样本"},
                        {"symbol": "600519.SH", "name": "贵州茅台"},
                    ],
                    scores={"300001.SZ": 88.0, "600519.SH": 66.0},
                ),
                "second_strategy": SimpleNamespace(
                    rows=[{"symbol": "600519.SH", "name": "贵州茅台"}],
                    scores={"600519.SH": 99.0},
                ),
            }

    client.app.state.strategy_engine = OrderedStrategyEngine()
    candidates, strategies = daily_review._screen_candidates(
        client.app.state,
        "2026-08-01",
        ["test_strategy", "second_strategy"],
    )

    assert [item["id"] for item in strategies] == ["test_strategy", "second_strategy"]
    assert [item["symbol"] for item in candidates] == ["300001.SZ", "600519.SH"]
    assert [item["rank"] for item in candidates] == [1, 2]
    assert "score" not in candidates[0]
    assert [match["id"] for match in candidates[1]["matched_strategies"]] == [
        "test_strategy",
        "second_strategy",
    ]
    assert candidates[1]["reason"].startswith("按策略池顺序首次来自“测试策略”第 2 名")


def test_hydrate_hides_legacy_candidate_consensus_without_rewriting_archive(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    client.post(
        "/api/daily-review/routines/2026-08-01/run",
        json={"strategy_ids": ["test_strategy"]},
    )
    legacy = daily_review._get_raw("2026-08-01")
    legacy["strategy_screening"].pop("selection_method")
    legacy["strategy_screening"]["ranking_method"] = (
        "match_count_then_reciprocal_rank_v1"
    )
    legacy_candidate = legacy["candidates"][0]
    legacy_candidate["score"] = 1.0
    for node in legacy_candidate["graph"]["nodes"]:
        node["input"]["fields"].append(
            {"key": "consensus_score", "label": "共识分", "value": 1.0}
        )
    daily_review._write_all([legacy])

    readable = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]
    candidate = readable["candidates"][0]
    assert readable["strategy_screening"]["selection_method"] == (
        "strategy_pool_order_then_result_order_v1"
    )
    assert "ranking_method" not in readable["strategy_screening"]
    assert "score" not in candidate
    assert all(
        field["key"] != "consensus_score"
        for node in candidate["graph"]["nodes"]
        for field in node["input"]["fields"]
    )
    stored = daily_review._get_raw("2026-08-01")
    assert stored["candidates"][0]["score"] == 1.0
    assert stored["strategy_screening"]["ranking_method"] == (
        "match_count_then_reciprocal_rank_v1"
    )


def test_historical_review_excludes_future_trades_and_future_news_from_every_agent(
    tmp_path, monkeypatch
):
    calls = {
        "market": 0,
        "positions": [],
        "news_items": [
            {
                "title": "贵州茅台 600519 截止日前新闻 PAST_NEWS",
                "summary": "可用于复盘日研究",
                "url": "https://example.com/past",
                "source": "测试源",
                "published_at": "2026-07-31T10:00:00+08:00",
            },
            {
                "title": "贵州茅台 600519 未来新闻 FUTURE_SENTINEL",
                "summary": "严禁注入历史上下文",
                "url": "https://example.com/future",
                "source": "测试源",
                "published_at": "2026-08-01T10:00:00+08:00",
            },
        ],
    }
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500, "2026-07-30")
    put_position(client, account["id"], "600519.SH", 50, 1700, "2026-08-01")

    accepted = client.post(
        "/api/daily-review/routines/2026-07-31/run",
        json={"strategy_ids": []},
    )
    routine = client.get("/api/daily-review/routines/2026-07-31").json()["routine"]

    assert accepted.status_code == 202
    assert routine["positions"][0]["quantity"] == 100
    assert routine["news_context"]["item_count"] == 1
    assert [item["title"] for item in calls["market_news"]] == [
        "贵州茅台 600519 截止日前新闻 PAST_NEWS"
    ]
    target_prompts = [
        item["user_prompt"]
        for item in calls["agent_prompts"]
        if item["source_ref"] == f"{account['id']}:600519.SH"
    ]
    assert target_prompts
    assert all("PAST_NEWS" in prompt for prompt in target_prompts)
    assert all("FUTURE_SENTINEL" not in prompt for prompt in target_prompts)
    assert routine["positions"][0]["graph"]["artifacts"]["news_evidence"][0][
        "title"
    ].endswith("PAST_NEWS")


def test_same_date_creates_independent_runs_and_later_edit_does_not_change_scope(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(
        client,
        account["id"],
        "600519.SH",
        100,
        1500,
        purchase_date="2026-07-30",
    )

    first = client.post("/api/daily-review/routines/2026-08-01/run")
    second = client.post("/api/daily-review/routines/2026-08-01/run")
    put_position(
        client,
        account["id"],
        "600519.SH",
        80,
        1520,
        purchase_date="2026-07-31",
    )
    stored = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]
    first_stored = client.get(
        f"/api/daily-review/routines/{first.json()['id']}"
    ).json()["routine"]
    history = client.get("/api/daily-review/routines").json()

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["run_number"] == 1
    assert second.json()["run_number"] == 2
    assert second.json()["status"] == "running"
    assert stored["id"] == second.json()["id"]
    assert stored["status"] == "completed"
    assert [item["id"] for item in history["items"]] == [
        second.json()["id"],
        first.json()["id"],
    ]
    assert history["running_count"] == 0
    assert calls["market"] == 2
    assert first_stored["market_review"]["report_id"] != stored["market_review"][
        "report_id"
    ]
    assert {
        report["daily_review_id"] for report in market_recap_reports.list_reports()
    } == {first.json()["id"], second.json()["id"]}
    position_source = f"{account['id']}:600519.SH"
    assert calls["agent_calls"].count((position_source, "market_analyst")) == 2
    assert {
        report["daily_review_id"]
        for report in stock_reports.list_reports()
        if report.get("source") == "daily_review"
    } == {first.json()["id"], second.json()["id"]}
    assert stored["positions"][0]["quantity"] == 100
    assert stored["positions"][0]["average_cost"] == 1500
    assert stored["positions"][0]["purchase_date"] == "2026-07-30"
    assert '"purchase_date": "2026-07-30"' in calls["target_prompts"][position_source]


def test_interrupt_all_marks_every_running_routine_and_pending_child(tmp_path, monkeypatch):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)
    first, _ = daily_review.prepare_routine(
        client.app.state.repo,
        date(2026, 8, 1),
    )
    second, _ = daily_review.prepare_routine(
        client.app.state.repo,
        date(2026, 8, 1),
    )

    response = client.post("/api/daily-review/routines/interrupt-all")

    assert response.status_code == 200
    assert response.json()["interrupted_count"] == 2
    assert set(response.json()["routine_ids"]) == {first["id"], second["id"]}
    for routine_id in (first["id"], second["id"]):
        routine = client.get(f"/api/daily-review/routines/{routine_id}").json()[
            "routine"
        ]
        assert routine["status"] == "interrupted"
        assert routine["cancel_requested_at"]
        assert routine["market_review"]["status"] == "interrupted"
        assert routine["strategy_screening"]["status"] == "interrupted"
        assert routine["positions"][0]["status"] == "interrupted"


def test_interrupt_all_cancels_the_registered_running_coroutine(tmp_path, monkeypatch):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    routine, _ = daily_review.prepare_routine(
        client.app.state.repo,
        date(2026, 8, 1),
    )

    async def scenario():
        started = asyncio.Event()

        async def blocking_news(app_state, routine_ref):
            daily_review._update_news(
                routine_ref,
                status="running",
                source_status="running",
            )
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr("app.services.daily_review._run_news", blocking_news)
        task = asyncio.create_task(
            daily_review.run_routine(client.app.state, routine["id"])
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        interrupted_ids = await daily_review.interrupt_all()
        await asyncio.wait_for(task, timeout=1)
        return interrupted_ids

    interrupted_ids = asyncio.run(scenario())
    stored = daily_review.get_routine(routine["id"])

    assert interrupted_ids == [routine["id"]]
    assert stored["status"] == "interrupted"
    assert stored["news_context"]["status"] == "interrupted"
    assert routine["id"] not in daily_review._ACTIVE_TASKS


def test_archive_keeps_report_bodies_after_shared_history_is_trimmed(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)
    client.post("/api/daily-review/routines/2026-08-01/run")
    stored = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]

    assert market_recap_reports.delete_report(stored["market_review"]["report_id"])
    assert stock_reports.delete_report(stored["positions"][0]["report_id"])

    restored = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]
    assert restored["market_review"]["report"]["content"].startswith("# 2026-08-01")
    assert restored["positions"][0]["report"]["content"].startswith(
        "# 600519.SH TradingAgents 研究结论"
    )
    assert "_report_snapshot" not in restored["market_review"]
    assert "_report_snapshot" not in restored["positions"][0]


def test_unknown_date_returns_empty_archive_without_creating_state(tmp_path, monkeypatch):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)

    response = client.get("/api/daily-review/routines/2026-07-31")

    assert response.status_code == 200
    assert response.json() == {"routine": None}
    assert calls == {"market": 0, "positions": []}


def test_existing_archive_is_upgraded_with_candidates_and_graphs(tmp_path, monkeypatch):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)
    daily_review.prepare_routine(client.app.state.repo, date(2026, 8, 1))

    legacy = daily_review._get_raw("2026-08-01")
    legacy.pop("strategy_screening")
    legacy.pop("candidates")
    legacy["positions"][0].pop("graph")
    legacy["positions"][0]["status"] = "completed"
    for field in ("trade_count", "realized_pnl", "total_fee", "total_tax"):
        legacy["scope_summary"].pop(field)
    daily_review._write_all([legacy])

    readable = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]
    assert readable["scope_summary"]["trade_count"] == 0
    assert readable["scope_summary"]["realized_pnl"] == 0

    response = client.post(
        "/api/daily-review/routines/2026-08-01/run",
        json={"strategy_ids": ["test_strategy"]},
    )
    upgraded = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]

    assert response.status_code == 202
    assert upgraded["status"] == "completed"
    assert upgraded["strategy_screening"]["status"] == "completed"
    assert upgraded["candidates"][0]["graph"]["status"] == "completed"
    assert upgraded["positions"][0]["graph"]["status"] == "completed"


def test_point_in_time_upgrade_invalidates_legacy_market_and_graph_outputs(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)
    daily_review.prepare_routine(client.app.state.repo, date(2026, 8, 1))

    legacy = daily_review._get_raw("2026-08-01")
    legacy["market_review"] = {
        "status": "completed",
        "report_id": "legacy-market-report",
        "_report_snapshot": {
            "id": "legacy-market-report",
            "as_of": "2026-08-01",
            "source": "daily_review",
            "content": "未经过 point-in-time 截断的旧报告",
        },
        "error": None,
    }
    legacy["news_context"]["status"] = "completed"
    legacy["strategy_screening"]["status"] = "completed"
    legacy_position = legacy["positions"][0]
    legacy_position["status"] = "completed"
    legacy_position["graph"]["schema_version"] = 3
    legacy_position["graph"]["status"] = "completed"
    legacy_position["report_id"] = "legacy-stock-report"
    legacy_position["_report_snapshot"] = {
        "id": "legacy-stock-report",
        "content": "未经过 point-in-time 截断的旧 Graph 报告",
    }
    daily_review._write_all([legacy])

    prepared, upgraded = daily_review.prepare_routine(
        client.app.state.repo, date(2026, 8, 1)
    )

    assert upgraded is True
    assert prepared["market_review"]["status"] == "pending"
    assert prepared["market_review"]["report_id"] is None
    assert prepared["positions"][0]["status"] == "pending"
    assert prepared["positions"][0]["graph"]["schema_version"] == 4
    assert prepared["positions"][0]["report_id"] is None


def test_market_failure_blocks_strategy_candidates_and_positions(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)

    async def failed_market(*args, **kwargs):
        calls["market"] += 1
        raise RuntimeError("市场数据暂不可用")

    monkeypatch.setattr("app.services.daily_review.recap_market_once", failed_market)

    response = client.post("/api/daily-review/routines/2026-08-01/run")
    routine = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]

    assert response.status_code == 202
    assert routine["status"] == "failed"
    assert routine["market_review"]["status"] == "failed"
    assert routine["market_review"]["error"] == "市场数据暂不可用"
    assert routine["strategy_screening"]["status"] == "blocked"
    assert routine["strategy_screening"]["error"] == "前置步骤“市场环境”未完成"
    assert routine["candidates"] == []
    assert routine["positions"][0]["status"] == "blocked"
    assert routine["positions"][0]["error"] == "前置步骤“市场环境”未完成"
    assert calls.get("agent_calls", []) == []


def test_retry_only_runs_failed_items_and_preserves_completed_report(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)
    put_position(client, account["id"], "000001.SZ", 1000, 10)

    failed_source = f"{account['id']}:000001.SZ"
    calls["fail_once"] = {f"{failed_source}:market_analyst"}
    client.post(
        "/api/daily-review/routines/2026-08-01/run",
        json={"strategy_ids": ["test_strategy"]},
    )
    first = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]
    completed = next(item for item in first["positions"] if item["symbol"] == "600519.SH")
    completed_report_id = completed["report_id"]
    retry = client.post("/api/daily-review/routines/2026-08-01/retry")
    final = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]

    assert retry.status_code == 202
    assert final["status"] == "completed"
    assert calls["agent_calls"].count(
        (f"{account['id']}:600519.SH", "market_analyst")
    ) == 1
    assert calls["agent_calls"].count((failed_source, "market_analyst")) == 2
    assert calls["agent_calls"].count((failed_source, "sentiment_analyst")) == 1
    assert calls["market"] == 1
    assert next(
        item for item in final["positions"] if item["symbol"] == "600519.SH"
    )["report_id"] == completed_report_id


def test_failed_graph_node_can_be_recovered_without_rerunning_completed_siblings(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)
    source_ref = f"{account['id']}:600519.SH"
    calls["fail_once"] = {f"{source_ref}:bear_researcher"}

    client.post(
        "/api/daily-review/routines/2026-08-01/run",
        json={"strategy_ids": ["test_strategy"]},
    )
    failed = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]
    position = failed["positions"][0]
    node_status = {node["id"]: node["status"] for node in position["graph"]["nodes"]}

    assert failed["status"] == "degraded"
    assert position["status"] == "failed"
    assert node_status["market_analyst"] == "completed"
    assert node_status["bull_researcher"] == "completed"
    assert node_status["bear_researcher"] == "failed"
    assert node_status["research_manager"] == "blocked"
    assert node_status["portfolio_manager"] == "blocked"
    failed_edges = {
        (edge["source"], edge["target"]): edge["status"]
        for edge in position["graph"]["edges"]
    }
    assert failed_edges[("bull_researcher", "bear_researcher")] == "failed"
    assert failed_edges[("bear_researcher", "bull_researcher")] == "blocked"

    recovered = client.post(
        "/api/daily-review/routines/2026-08-01/graph/retry",
        json={
            "target_type": "position",
            "source_ref": source_ref,
            "node_id": "bear_researcher",
        },
    )
    final = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]

    assert recovered.status_code == 202
    assert final["status"] == "completed"
    assert final["positions"][0]["status"] == "completed"
    assert calls["agent_calls"].count((source_ref, "market_analyst")) == 1
    assert calls["agent_calls"].count((source_ref, "bull_researcher")) == 2
    assert calls["agent_calls"].count((source_ref, "bear_researcher")) == 3
    assert calls["agent_calls"].count((source_ref, "research_manager")) == 1
    assert calls["agent_calls"].count((source_ref, "portfolio_manager")) == 1

    completed_retry = client.post(
        "/api/daily-review/routines/2026-08-01/graph/retry",
        json={
            "target_type": "position",
            "source_ref": source_ref,
            "node_id": "market_analyst",
        },
    )
    assert completed_retry.status_code == 409


def test_candidate_graph_retry_releases_and_runs_blocked_position_step(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)
    candidate_ref = "candidate:300001.SZ"
    position_ref = f"{account['id']}:600519.SH"
    calls["fail_once"] = {f"{candidate_ref}:market_analyst"}

    client.post(
        "/api/daily-review/routines/2026-08-01/run",
        json={"strategy_ids": ["test_strategy"]},
    )
    failed = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]

    assert failed["candidates"][0]["status"] == "failed"
    assert failed["positions"][0]["status"] == "blocked"
    assert failed["positions"][0]["error"] == "前置步骤“策略候选”未完成"
    assert calls["agent_calls"].count((position_ref, "market_analyst")) == 0

    recovered = client.post(
        "/api/daily-review/routines/2026-08-01/graph/retry",
        json={
            "target_type": "candidate",
            "source_ref": candidate_ref,
            "node_id": "market_analyst",
        },
    )
    final = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]

    assert recovered.status_code == 202
    assert final["status"] == "completed"
    assert final["candidates"][0]["status"] == "completed"
    assert final["positions"][0]["status"] == "completed"
    assert calls["agent_calls"].count((candidate_ref, "market_analyst")) == 2
    assert calls["agent_calls"].count((position_ref, "market_analyst")) == 1


def test_second_round_bull_failure_resumes_current_debate_turn_and_keeps_history(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)
    source_ref = f"{account['id']}:600519.SH"
    calls["fail_on_call"] = {f"{source_ref}:bull_researcher": 2}

    client.post("/api/daily-review/routines/2026-08-01/run")
    failed = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]
    graph = failed["positions"][0]["graph"]
    debate = graph["debates"]["research"]

    assert failed["status"] == "degraded"
    assert debate["status"] == "failed"
    assert debate["completed_turns"] == 2
    assert debate["current_round"] == 2
    assert debate["current_speaker_id"] == "bull_researcher"
    assert [turn["status"] for turn in debate["history"]] == [
        "completed",
        "completed",
        "failed",
    ]
    preserved_turn_ids = [turn["id"] for turn in debate["history"][:2]]

    recovered = client.post(
        "/api/daily-review/routines/2026-08-01/graph/retry",
        json={
            "target_type": "position",
            "source_ref": source_ref,
            "node_id": "bull_researcher",
        },
    )
    final = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]
    final_graph = final["positions"][0]["graph"]
    final_debate = final_graph["debates"]["research"]

    assert recovered.status_code == 202
    assert final["status"] == "completed"
    assert final_debate["status"] == "completed"
    assert final_debate["completed_turns"] == 4
    assert [turn["id"] for turn in final_debate["history"][:2]] == preserved_turn_ids
    assert [turn["status"] for turn in final_debate["history"]] == [
        "completed",
        "completed",
        "failed",
        "completed",
        "completed",
    ]
    assert calls["agent_calls"].count((source_ref, "bull_researcher")) == 3
    assert calls["agent_calls"].count((source_ref, "bear_researcher")) == 2
    assert calls["agent_calls"].count((source_ref, "research_manager")) == 1
    manager_prompt = next(
        item["user_prompt"]
        for item in calls["agent_prompts"]
        if item["source_ref"] == source_ref and item["role"] == "research_manager"
    )
    assert "完整多空辩论记录" in manager_prompt
    assert "bear_researcher 第 2 次" in manager_prompt


def test_recovery_marks_unfinished_work_interrupted(tmp_path, monkeypatch):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)
    daily_review.prepare_routine(client.app.state.repo, date(2026, 8, 1))

    recovered = daily_review.recover_interrupted()
    routine = daily_review.get_routine("2026-08-01")

    assert recovered == 1
    assert routine["status"] == "interrupted"
    assert routine["market_review"]["status"] == "interrupted"
    assert routine["positions"][0]["status"] == "interrupted"


def test_recovery_finalizes_terminal_children_after_crash_before_summary(
    tmp_path, monkeypatch
):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)
    monkeypatch.setattr("app.services.daily_review._finalize", lambda business_date: None)

    client.post("/api/daily-review/routines/2026-08-01/run")
    before = daily_review.get_routine("2026-08-01")
    recovered = daily_review.recover_interrupted()
    after = daily_review.get_routine("2026-08-01")

    assert before["status"] == "running"
    assert before["market_review"]["status"] == "completed"
    assert before["positions"][0]["status"] == "completed"
    assert recovered == 1
    assert after["status"] == "completed"


def test_daily_report_from_another_account_is_not_reused(tmp_path, monkeypatch):
    calls = {"market": 0, "positions": []}
    client = make_client(tmp_path, monkeypatch, calls)
    account = create_account(client)
    put_position(client, account["id"], "600519.SH", 100, 1500)
    foreign = stock_reports.save_report(
        {
            "symbol": "600519.SH",
            "name": "贵州茅台",
            "focus": "每日复盘中的持仓客观分析",
            "content": "# 另一个账户的报告",
            "source": "daily_review",
            "source_ref": "other-account:600519.SH",
            "daily_review_date": "2026-08-01",
        }
    )

    client.post("/api/daily-review/routines/2026-08-01/run")
    routine = client.get("/api/daily-review/routines/2026-08-01").json()["routine"]

    assert calls["agent_calls"].count(
        (f"{account['id']}:600519.SH", "market_analyst")
    ) == 1
    assert routine["positions"][0]["report_id"] != foreign["id"]
    assert routine["positions"][0]["report"]["content"].startswith(
        "# 600519.SH TradingAgents 研究结论"
    )
