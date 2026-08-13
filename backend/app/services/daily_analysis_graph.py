"""每日复盘中参考 TradingAgents 的可观察研究 Graph。"""
from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.ai_provider import generate_ai_text
from app.services.daily_analysis_schemas import (
    parse_structured_output,
    render_structured_output,
    response_format_for,
    structured_model_for,
    structured_output_instruction,
)
from app.services.stock_analyzer import StockAnalysisInput, build_stock_analysis_input

_TIMEZONE = ZoneInfo("Asia/Shanghai")
_SCHEMA_VERSION = 4
_MAX_NODE_OUTPUT = 8000
_MAX_UPSTREAM_PROMPT = 3600
_MAX_EVENTS = 120
_MAX_RESEARCH_DEBATE_ROUNDS = 2
_RESEARCHER_NODE_IDS = ("bull_researcher", "bear_researcher")

_GROUPS = (
    {
        "id": "context",
        "label": "研究上下文",
        "description": "装配行情、指标、关键价位和目标身份事实",
        "order": 0,
    },
    {
        "id": "analyst_team",
        "label": "Analyst Team",
        "description": "从市场、情绪、新闻和基本面四个视角独立研究",
        "order": 1,
    },
    {
        "id": "research_team",
        "label": "Researcher Team",
        "description": "Bull / Bear 进行两轮交替辩论,由 Research Manager 裁决",
        "order": 2,
    },
    {
        "id": "proposal",
        "label": "Research Plan",
        "description": "Trader 只整理研究观察方案,不生成交易指令",
        "order": 3,
    },
    {
        "id": "risk_team",
        "label": "Risk Management",
        "description": "激进、中性、保守三个风险视角依次审查",
        "order": 4,
    },
    {
        "id": "decision",
        "label": "Research Decision",
        "description": "Portfolio Manager 形成最终研究结论,到此结束,不执行交易",
        "order": 5,
    },
)

_NODE_DEFINITIONS = (
    {
        "id": "facts",
        "label": "研究事实",
        "kind": "input",
        "team_id": "context",
        "description": "冻结目标身份并装配可追溯的行情、指标和关键价位",
    },
    {
        "id": "market_analyst",
        "label": "Market Analyst",
        "kind": "analyst",
        "team_id": "analyst_team",
        "description": "检查价格趋势、量价、波动和关键价位",
    },
    {
        "id": "sentiment_analyst",
        "label": "Sentiment Analyst",
        "kind": "analyst",
        "team_id": "analyst_team",
        "description": "只从已提供材料识别情绪线索和共识拥挤风险",
    },
    {
        "id": "news_analyst",
        "label": "News Analyst",
        "kind": "analyst",
        "team_id": "analyst_team",
        "description": "核对事件、新闻和宏观材料,缺失时明确记录数据缺口",
    },
    {
        "id": "fundamentals_analyst",
        "label": "Fundamentals Analyst",
        "kind": "analyst",
        "team_id": "analyst_team",
        "description": "检查财务和基本面事实,不用价格表现替代基本面证据",
    },
    {
        "id": "bull_researcher",
        "label": "Bull Researcher",
        "kind": "researcher",
        "team_id": "research_team",
        "description": "提出支持性证据并逐轮回应 Bear 的上一轮反证",
    },
    {
        "id": "bear_researcher",
        "label": "Bear Researcher",
        "kind": "researcher",
        "team_id": "research_team",
        "description": "逐轮质询 Bull 论点,提出反证、脆弱点和失败条件",
    },
    {
        "id": "research_manager",
        "label": "Research Manager",
        "kind": "manager",
        "team_id": "research_team",
        "description": "裁决多空分歧,形成有证据等级的研究结论",
    },
    {
        "id": "trader",
        "label": "Trader · 研究方案",
        "kind": "planner",
        "team_id": "proposal",
        "description": "把研究结论整理成后续观察方案,不生成交易动作",
    },
    {
        "id": "aggressive_risk",
        "label": "Aggressive Analyst",
        "kind": "risk",
        "team_id": "risk_team",
        "description": "检验在积极假设下仍必须满足的证据条件",
    },
    {
        "id": "conservative_risk",
        "label": "Conservative Analyst",
        "kind": "risk",
        "team_id": "risk_team",
        "description": "以资本保护视角寻找尾部风险和证据断点",
    },
    {
        "id": "neutral_risk",
        "label": "Neutral Analyst",
        "kind": "risk",
        "team_id": "risk_team",
        "description": "平衡积极与保守意见,列出仍需观察的事实",
    },
    {
        "id": "portfolio_manager",
        "label": "Portfolio Manager · 研究结论",
        "kind": "manager",
        "team_id": "decision",
        "description": "汇总全部证据并给出研究状态,到此结束,不连接执行",
    },
)
_NODE_BY_ID = {item["id"]: item for item in _NODE_DEFINITIONS}

_LAYOUT_POSITIONS = {
    "facts": {"x": 72, "y": 300},
    "market_analyst": {"x": 260, "y": 78},
    "sentiment_analyst": {"x": 260, "y": 222},
    "news_analyst": {"x": 260, "y": 378},
    "fundamentals_analyst": {"x": 260, "y": 522},
    "bull_researcher": {"x": 470, "y": 190},
    "bear_researcher": {"x": 470, "y": 410},
    "research_manager": {"x": 640, "y": 300},
    "trader": {"x": 795, "y": 300},
    "aggressive_risk": {"x": 960, "y": 140},
    "conservative_risk": {"x": 960, "y": 300},
    "neutral_risk": {"x": 960, "y": 460},
    "portfolio_manager": {"x": 1155, "y": 300},
}

_ANALYST_NODE_IDS = (
    "market_analyst",
    "sentiment_analyst",
    "news_analyst",
    "fundamentals_analyst",
)
_SEQUENTIAL_NODE_IDS = (
    "research_manager",
    "trader",
    "aggressive_risk",
    "conservative_risk",
    "neutral_risk",
    "portfolio_manager",
)

_EDGES = (
    *(
        {
            "source": "facts",
            "target": node_id,
            "label": "事实输入",
            "kind": "flow",
            "dependency": True,
        }
        for node_id in _ANALYST_NODE_IDS
    ),
    *(
        {
            "source": node_id,
            "target": researcher,
            "label": "分析报告",
            "kind": "evidence",
            "dependency": True,
        }
        for node_id in _ANALYST_NODE_IDS
        for researcher in ("bull_researcher", "bear_researcher")
    ),
    {
        "source": "bull_researcher",
        "target": "bear_researcher",
        "label": "正方论证",
        "kind": "debate",
        "dependency": True,
    },
    {
        "source": "bear_researcher",
        "target": "bull_researcher",
        "label": "反方质询",
        "kind": "feedback",
        "dependency": False,
    },
    {
        "source": "bull_researcher",
        "target": "research_manager",
        "label": "支持证据",
        "kind": "evidence",
        "dependency": True,
    },
    {
        "source": "bear_researcher",
        "target": "research_manager",
        "label": "反方证据",
        "kind": "evidence",
        "dependency": True,
    },
    {
        "source": "research_manager",
        "target": "trader",
        "label": "研究裁决",
        "kind": "flow",
        "dependency": True,
    },
    {
        "source": "trader",
        "target": "aggressive_risk",
        "label": "研究方案",
        "kind": "flow",
        "dependency": True,
    },
    {
        "source": "aggressive_risk",
        "target": "conservative_risk",
        "label": "积极观点",
        "kind": "debate",
        "dependency": True,
    },
    {
        "source": "conservative_risk",
        "target": "neutral_risk",
        "label": "保守质询",
        "kind": "debate",
        "dependency": True,
    },
    {
        "source": "neutral_risk",
        "target": "aggressive_risk",
        "label": "平衡反馈",
        "kind": "feedback",
        "dependency": False,
    },
    *(
        {
            "source": node_id,
            "target": "portfolio_manager",
            "label": "风险意见",
            "kind": "evidence",
            "dependency": True,
        }
        for node_id in (
            "aggressive_risk",
            "conservative_risk",
            "neutral_risk",
        )
    ),
)

_COMMON_GUARDRAIL = """
你只能基于输入事实做客观研究。不得输出买入、卖出、加仓、减仓、止损、止盈、仓位、目标价或任何交易执行指令;不得把缺失数据补成事实。结论必须区分数据事实、推断和未知项。不得因角色立场而篡改、忽略或虚构证据。
""".strip()

_NODE_PROMPTS = {
    "market_analyst": "检查趋势、均线、量价、波动与关键价位。输出:市场观察、数值证据、未知项。",
    "sentiment_analyst": "检查输入中真实存在的情绪、共识和拥挤线索。没有社交或情绪数据时必须明确写数据缺口。输出:情绪线索、反向风险、数据缺口。",
    "news_analyst": "检查输入中真实存在的新闻、事件和宏观材料。没有新闻材料时不得使用常识补写。输出:已知事件、潜在影响、数据缺口。",
    "fundamentals_analyst": "检查财务与基本面材料。不得用技术走势冒充基本面。输出:基本面事实、质量风险、数据缺口。",
    "bull_researcher": (
        "你是支持性研究论证方(Bull Researcher)。你的任务是形成有说服力、以证据为基础的支持性论证,"
        "重点识别增长潜力、竞争优势和积极指标,并以四类 Analyst 报告中的具体事实支撑观点。"
        "你必须主动回应 Bear 的上一轮反证,指出其论证可能忽略的事实、条件或时间边界;首轮没有对方发言时,"
        "明确说明并建立初始论证。使用直接、对话式的论辩表达,不能只罗列数据。保持明确立场,"
        "但该立场只代表研究证据方向,不代表任何投资或执行建议。"
    ),
    "bear_researcher": (
        "你是反方研究论证方(Bear Researcher)。你的任务是形成有说服力、以证据为基础的反方论证,"
        "重点识别风险、挑战、竞争弱点和消极指标,并检验支持性叙事中的脆弱假设。"
        "你必须逐项回应 Bull 的上一轮观点,使用冻结事实提出反证和可验证的失效条件,不能退化成与辩论无关的风险清单。"
        "使用直接、对话式的论辩表达,揭示过度乐观假设。保持明确立场,但该立场只代表研究证据方向,"
        "不代表任何投资或执行建议。"
    ),
    "research_manager": (
        "你是研究经理兼辩论主持人(Research Manager)。阅读完整 Bull / Bear 两轮交锋,评估双方最强证据和最弱假设,"
        "不按篇幅、语气或观点数量投票。你必须对关键证据逐项给出高、中、低等级,并形成明确研究裁决。"
        "研究状态量表仅允许:支持性证据占优、反证占优、证据均衡、证据不足、数据冲突。"
        "只有在双方证据确实接近时才能选择证据均衡,不能用中性结论逃避裁决。"
    ),
    "trader": (
        "你是研究方案代理(Trader)。参考 TradingAgents Trader 将上游裁决转化为清晰方案的职责,"
        "但在本系统中只能形成非交易性的后续研究方案。结合研究裁决,给出当前研究状态、可验证的后续观察项,"
        "以及让结论升级或失效所需的新事实。不得把计划写成任何交易动作或执行参数。"
    ),
    "aggressive_risk": (
        "你是积极风险视角(Aggressive Analyst)。像 TradingAgents 的积极风险分析者一样主动寻找高不确定性环境中的潜在上行解释,"
        "挑战过度保守的判断,并说明哪些积极因素值得继续研究。同时必须列明这些判断成立的证据门槛和可能被放大的风险,"
        "若材料中已有 Conservative 或 Neutral 的观点,必须逐项回应其担忧,指出其谨慎假设可能遗漏的机会或证据;"
        "若尚无其他风险观点,则基于已有材料建立初始论证。侧重论辩和说服,不能只罗列数据,也不得把高风险偏好转换为交易动作。"
    ),
    "conservative_risk": (
        "你是保守风险视角(Conservative Analyst)。以资本保护和稳健性为核心,审查尾部风险、数据断点、最弱证据和过度乐观假设。"
        "重点关注潜在损失、经济下行和市场波动,说明哪些未知项会让研究结论不可靠,以及哪些数据缺口在形成更强结论前不可接受。"
        "若材料中已有 Aggressive 或 Neutral 的观点,必须逐项质询其可能忽略的威胁和可持续性问题;"
        "若尚无其他风险观点,则基于已有材料建立初始论证。只做研究风险审查,不提供执行方案。"
    ),
    "neutral_risk": (
        "你是中性风险视角(Neutral Analyst)。平衡积极与保守意见,不机械折中;识别双方共同承认的风险、真正的分歧风险,"
        "并结合更广泛的市场趋势、潜在经济变化、相关性与集中度因素,提炼能够最小成本缩小不确定性的观察集。"
        "必须同时挑战 Aggressive 的过度乐观和 Conservative 的过度谨慎,逐项指出各自论证的弱点。"
        "你的平衡必须建立在证据权重上,不能为了中立而回避明确判断,也不能只罗列数据。"
    ),
    "portfolio_manager": (
        "你是研究组合经理(Portfolio Manager)。综合 Research Manager 的裁决、研究方案和三个风险视角,"
        "形成最终研究结论,明确证据共识、关键分歧、风险边界、仍需观察的事实和单一研究状态。"
        "研究状态只能从支持性证据占优、反证占优、证据均衡、证据不足、数据冲突中选择。"
        "结论必须果断,每一项判断都要落到 Analyst 材料或风险论辩中的具体证据。"
        "这是研究流程的终点;不得连接订单、账户或任何交易执行。"
    ),
}

_USER_PROMPT_TEMPLATE = """{冻结的标的研究事实}

以下是本节点可以使用的上游研究材料:
```json
{按依赖边汇总的上游节点输出}
```

只使用上述事实和材料;上游为空或缺少某类数据时,明确写出未知项。"""

_DEBATE_USER_PROMPT_TEMPLATE = f"""{{冻结的标的研究事实}}

以下是四类 Analyst 的上游研究材料:
```json
{{Analyst 节点输出}}
```

当前是第 {{当前轮次}}/{_MAX_RESEARCH_DEBATE_ROUNDS} 轮,第 {{当前发言序号}}/{2 * _MAX_RESEARCH_DEBATE_ROUNDS} 次发言。
当前发言方: {{Bull Researcher 或 Bear Researcher}}
对方上一轮观点:
```json
{{对方上一轮结构化观点;首轮 Bull 为空}}
```
完整多空辩论记录:
```json
{{此前全部有效发言}}
```

必须直接回应对方上一轮观点;只使用冻结事实、Analyst 材料和辩论记录。"""

_RESEARCH_MANAGER_USER_PROMPT_TEMPLATE = """{冻结的标的研究事实}

以下是 Bull 与 Bear 的最后一次节点输出:
```json
{Bull Researcher 与 Bear Researcher 输出}
```

完整多空辩论记录(按真实发言顺序):
```json
{两轮、四次有效发言}
```

必须裁决完整记录中的逐轮交锋,不能只读取双方最后一次摘要。"""

_OUTPUT_REQUIREMENTS = {
    "facts": ("已装配事实",),
    "market_analyst": ("市场观察", "数值证据", "未知项"),
    "sentiment_analyst": ("情绪线索", "反向风险", "数据缺口"),
    "news_analyst": ("已知事件", "潜在影响", "数据缺口"),
    "fundamentals_analyst": ("基本面事实", "质量风险", "数据缺口"),
    "bull_researcher": ("支持观点", "对 Bear 的回应", "证据链", "成立前提"),
    "bear_researcher": ("主要反证", "对 Bull 的回应", "脆弱假设", "论证失效条件"),
    "research_manager": ("已确认共识", "未解决分歧", "证据等级", "研究裁决"),
    "trader": ("研究状态", "后续观察清单", "结论升级或失效所需事实"),
    "aggressive_risk": ("可支持因素", "必要条件", "放大风险"),
    "conservative_risk": ("尾部风险", "最弱证据", "不可接受的数据缺口"),
    "neutral_risk": ("共同风险", "分歧风险", "最小后续观察集"),
    "portfolio_manager": (
        "综合结论",
        "证据共识",
        "关键分歧",
        "风险边界",
        "仍需观察的事实",
        "研究状态",
    ),
}

_FIELD_LABELS = {
    "source_ref": "目标编号",
    "account_id": "账户编号",
    "account_name": "账户",
    "symbol": "标的代码",
    "quantity": "数量",
    "average_cost": "平均成本",
    "purchase_date": "参考买入日期",
    "total_cost": "总成本",
    "current_price": "当日收盘价",
    "price_date": "价格日期",
    "market_value": "市值",
    "unrealized_pnl": "浮动盈亏",
    "unrealized_return_ratio": "浮动收益率",
    "note": "备注",
    "rank": "候选排名",
    "matched_strategies": "命中策略",
    "reason": "候选原因",
    "news_evidence": "截至复盘日的新闻证据",
}


def _now_iso() -> str:
    return datetime.now(_TIMEZONE).isoformat(timespec="seconds")


def _research_debate_definition() -> dict:
    max_turns = 2 * _MAX_RESEARCH_DEBATE_ROUNDS
    return {
        "id": "research",
        "label": "Bull / Bear Research Debate",
        "participants": list(_RESEARCHER_NODE_IDS),
        "speaker_order": list(_RESEARCHER_NODE_IDS),
        "max_rounds": _MAX_RESEARCH_DEBATE_ROUNDS,
        "max_turns": max_turns,
        "stop_condition": (
            f"完成 {_MAX_RESEARCH_DEBATE_ROUNDS} 轮、共 {max_turns} 次交替发言后"
            "进入 Research Manager"
        ),
    }


def _new_research_debate() -> dict:
    return {
        **_research_debate_definition(),
        "status": "pending",
        "completed_turns": 0,
        "current_round": 1,
        "current_speaker_id": "bull_researcher",
        "history": [],
        "error": None,
        "started_at": None,
        "completed_at": None,
    }


def _new_node(definition: dict) -> dict:
    return {
        **definition,
        "position": copy.deepcopy(_LAYOUT_POSITIONS[definition["id"]]),
        "status": "pending",
        "attempt": 0,
        "input": None,
        "output": None,
        "error": None,
        "started_at": None,
        "completed_at": None,
        "turns": [],
    }


def _system_prompt(node_id: str) -> str:
    node = _NODE_BY_ID[node_id]
    return (
        f"角色标识: {node_id}\n"
        f"你是 TradingAgents 研究框架中的 {node['label']}。"
        f"{_NODE_PROMPTS[node_id]}\n{_COMMON_GUARDRAIL}\n"
        f"{structured_output_instruction(node_id)}"
    )


def _user_prompt_template(node_id: str) -> str:
    if node_id in _RESEARCHER_NODE_IDS:
        return _DEBATE_USER_PROMPT_TEMPLATE
    if node_id == "research_manager":
        return _RESEARCH_MANAGER_USER_PROMPT_TEMPLATE
    return _USER_PROMPT_TEMPLATE


def _required_inputs(node_id: str) -> list[dict]:
    if node_id == "facts":
        return [
            {
                "id": "target_context",
                "label": "研究目标上下文",
                "source": "daily_review_snapshot",
                "required": True,
                "description": "冻结持仓身份或策略候选身份,包含日期、代码和可用账户事实。",
            },
            {
                "id": "market_history",
                "label": "行情与技术指标",
                "source": "local_market_repository",
                "required": True,
                "description": "截至复盘日的最近日 K、OHLCV 和已计算技术指标。",
            },
            {
                "id": "key_levels",
                "label": "关键价位",
                "source": "level_calculator",
                "required": True,
                "description": "基于已冻结行情计算的支撑、压力和价位结构。",
            },
            {
                "id": "financial_context",
                "label": "财务材料",
                "source": "local_financial_repository",
                "required": False,
                "description": "最近可用财务指标和利润表;缺失时保留为空,不得补写。",
            },
            {
                "id": "news_evidence",
                "label": "截至复盘日的新闻证据",
                "source": "review_news_archive",
                "required": False,
                "description": (
                    "仅包含发布时间不晚于复盘日的归档新闻;未知发布时间和未来新闻已排除。"
                ),
            },
        ]

    inputs = [
        {
            "id": "frozen_facts",
            "label": "冻结研究事实",
            "source": "facts",
            "required": True,
            "description": "标的、日期、行情、指标、价位、财务材料和研究目标上下文。",
        }
    ]
    upstream_ids = [
        str(edge["source"])
        for edge in _EDGES
        if edge["target"] == node_id
        and edge.get("dependency", True)
        and edge["source"] != "facts"
        and not (
            node_id in _RESEARCHER_NODE_IDS
            and edge["source"] in _RESEARCHER_NODE_IDS
        )
    ]
    inputs.extend(
        {
            "id": upstream_id,
            "label": f"{_NODE_BY_ID[upstream_id]['label']} 输出",
            "source": upstream_id,
            "required": True,
            "description": _NODE_BY_ID[upstream_id]["description"],
        }
        for upstream_id in dict.fromkeys(upstream_ids)
    )
    if node_id in _RESEARCHER_NODE_IDS:
        inputs.append(
            {
                "id": "research_debate_history",
                "label": "多空辩论历史与对方上一轮观点",
                "source": "research_debate.history",
                "required": node_id == "bear_researcher",
                "description": (
                    "第一轮 Bull 发言时为空;后续发言必须读取完整历史并直接回应"
                    "对方上一轮观点。"
                ),
            }
        )
    elif node_id == "research_manager":
        inputs.append(
            {
                "id": "research_debate_history",
                "label": "完整多空辩论记录",
                "source": "research_debate.history",
                "required": True,
                "description": "两轮 Bull / Bear 交替发言的完整结构化记录。",
            }
        )
    return inputs


def graph_definition() -> dict:
    """返回设置页和运行态共同使用的只读 Graph 定义。"""
    nodes = []
    for definition in _NODE_DEFINITIONS:
        node_id = definition["id"]
        invokes_model = node_id != "facts"
        nodes.append(
            {
                **copy.deepcopy(definition),
                "position": copy.deepcopy(_LAYOUT_POSITIONS[node_id]),
                "prompt": {
                    "invokes_model": invokes_model,
                    "system": _system_prompt(node_id) if invokes_model else "",
                    "user_template": _user_prompt_template(node_id) if invokes_model else "",
                    "response_format": response_format_for(node_id) if invokes_model else None,
                },
                "required_inputs": _required_inputs(node_id),
                "required_outputs": [
                    {
                        "id": f"section_{index}",
                        "label": label,
                        "format": "markdown_section",
                        "required": True,
                    }
                    for index, label in enumerate(
                        _OUTPUT_REQUIREMENTS[node_id],
                        start=1,
                    )
                ],
            }
        )
    return {
        "schema_version": _SCHEMA_VERSION,
        "framework": "TradingAgents",
        "mode": "research_only",
        "execution_enabled": False,
        "description": "参考 TradingAgents 的研究流程,到 Portfolio Manager 结束,不连接交易执行。",
        "debates": {"research": _research_debate_definition()},
        "groups": copy.deepcopy(list(_GROUPS)),
        "nodes": nodes,
        "edges": [copy.deepcopy(edge) for edge in _EDGES],
    }


def new_graph(*, target_type: str, target_ref: str, symbol: str) -> dict:
    """创建 TradingAgents 风格、但只做研究且不连接执行的固定拓扑。"""
    graph_id = (
        target_ref
        if target_ref.startswith(f"{target_type}:")
        else f"{target_type}:{target_ref}"
    )
    graph = {
        "schema_version": _SCHEMA_VERSION,
        "framework": "TradingAgents",
        "mode": "research_only",
        "id": graph_id,
        "target_type": target_type,
        "target_ref": target_ref,
        "symbol": symbol,
        "status": "pending",
        "groups": copy.deepcopy(list(_GROUPS)),
        "nodes": [_new_node(item) for item in _NODE_DEFINITIONS],
        "edges": [
            {**copy.deepcopy(edge), "status": "pending"}
            for edge in _EDGES
        ],
        "events": [],
        "debates": {"research": _new_research_debate()},
        "progress": {},
        "artifacts": {},
        "updated_at": _now_iso(),
    }
    _refresh_runtime(graph)
    return graph


def needs_upgrade(graph: dict | None) -> bool:
    return not graph or int(graph.get("schema_version") or 1) < _SCHEMA_VERSION


def _node(graph: dict, node_id: str) -> dict:
    match = next((item for item in graph.get("nodes", []) if item.get("id") == node_id), None)
    if match is None:
        raise KeyError(f"分析节点不存在: {node_id}")
    return match


def _node_markdown(node: dict) -> str:
    output = node.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        return str(output.get("markdown") or output.get("summary") or "")
    return ""


def _research_debate(graph: dict) -> dict:
    debates = graph.setdefault("debates", {})
    debate = debates.get("research")
    if not isinstance(debate, dict):
        debate = _new_research_debate()
        debates["research"] = debate
    return debate


def _completed_debate_turns(debate: dict) -> list[dict]:
    return [turn for turn in debate.get("history", []) if turn.get("status") == "completed"]


def _next_debate_position(debate: dict) -> tuple[int, int, str] | None:
    completed_turns = len(_completed_debate_turns(debate))
    max_turns = int(debate.get("max_turns") or 0)
    if completed_turns >= max_turns:
        return None
    speaker_id = _RESEARCHER_NODE_IDS[completed_turns % len(_RESEARCHER_NODE_IDS)]
    round_number = completed_turns // len(_RESEARCHER_NODE_IDS) + 1
    return completed_turns + 1, round_number, speaker_id


def _latest_completed_turn(debate: dict, speaker_id: str | None = None) -> dict | None:
    return next(
        (
            turn
            for turn in reversed(debate.get("history", []))
            if turn.get("status") == "completed"
            and (speaker_id is None or turn.get("speaker_id") == speaker_id)
        ),
        None,
    )


def _plain_summary(markdown: str) -> str:
    lines = [
        line.strip()
        for line in markdown.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "```", ">"))
    ]
    text = " ".join(lines)
    if not text:
        text = re.sub(r"^[#>*`\-\s]+", "", markdown.strip())
    return text[:280]


def _structured_output(markdown: str) -> dict:
    text = markdown.strip()
    sections: list[dict] = []
    title = "分析结果"
    buffer: list[str] = []
    for line in text.splitlines():
        heading = re.match(r"^#{1,4}\s+(.+?)\s*$", line)
        if heading:
            content = "\n".join(buffer).strip()
            if content:
                sections.append({"title": title, "content": content})
            title = heading.group(1).strip()
            buffer = []
        else:
            buffer.append(line)
    content = "\n".join(buffer).strip()
    if content or not sections:
        sections.append({"title": title, "content": content or text})
    return {
        "summary": _plain_summary(text),
        "sections": sections,
        "markdown": text,
    }


def _validated_model_output(node_id: str, raw_output: str) -> dict:
    """校验 structured output,并转换为现有前端可消费的展示结构。"""
    model = structured_model_for(node_id)
    if model is None:
        return _structured_output(_truncate(raw_output))
    validated = parse_structured_output(node_id, raw_output)
    markdown = _truncate(render_structured_output(validated))
    result = _structured_output(markdown)
    result.update(
        schema=type(validated).__name__,
        data=validated.model_dump(mode="json"),
    )
    return result


def hydrate_graph(graph: dict | None) -> dict | None:
    """为旧档案补展示默认值,不在读取时触发重跑或改写持久化。"""
    if graph is None:
        return None
    output = copy.deepcopy(graph)
    output.setdefault("schema_version", 1)
    output.setdefault("framework", "Legacy")
    output.setdefault("mode", "research_only")
    output.setdefault("groups", [])
    output.setdefault("events", [])
    if int(output.get("schema_version") or 1) >= _SCHEMA_VERSION:
        output.setdefault("debates", {"research": _new_research_debate()})
    for node in output.get("nodes", []):
        node.setdefault("team_id", "legacy")
        node.setdefault("description", "旧版分析节点")
        node.setdefault("input", None)
        node.setdefault("turns", [])
        if isinstance(node.get("output"), str):
            node["output"] = _structured_output(node["output"])
    for edge in output.get("edges", []):
        edge.setdefault("label", "数据流")
        edge.setdefault("kind", "flow")
        edge.setdefault("dependency", True)
        edge.setdefault("status", "pending")
    _refresh_runtime(output)
    return output


def _add_event(graph: dict, node_id: str, status: str, message: str) -> None:
    events = graph.setdefault("events", [])
    sequence = max((int(item.get("sequence") or 0) for item in events), default=0) + 1
    definition = _NODE_BY_ID.get(node_id, {"label": node_id, "team_id": "context"})
    events.append(
        {
            "id": f"{graph['id']}:{sequence}",
            "sequence": sequence,
            "node_id": node_id,
            "node_label": definition["label"],
            "team_id": definition["team_id"],
            "status": status,
            "message": message,
            "at": _now_iso(),
        }
    )
    if len(events) > _MAX_EVENTS:
        del events[:-_MAX_EVENTS]


def _refresh_runtime(graph: dict) -> None:
    for node in graph.get("nodes", []):
        node_id = str(node.get("id") or "")
        definition = _NODE_BY_ID.get(node_id)
        if definition is None:
            continue
        node.setdefault("team_id", definition["team_id"])
        node.setdefault("description", definition["description"])
        node.setdefault("position", copy.deepcopy(_LAYOUT_POSITIONS[node_id]))
        node.setdefault("turns", [])
    if int(graph.get("schema_version") or 1) >= _SCHEMA_VERSION:
        debate = _research_debate(graph)
        for speaker_id in _RESEARCHER_NODE_IDS:
            _node(graph, speaker_id)["turns"] = [
                copy.deepcopy(turn)
                for turn in debate.get("history", [])
                if turn.get("speaker_id") == speaker_id
            ]
    nodes = {str(item.get("id")): item for item in graph.get("nodes", [])}
    for edge in graph.get("edges", []):
        source = nodes.get(str(edge.get("source")), {})
        target = nodes.get(str(edge.get("target")), {})
        source_status = source.get("status")
        target_status = target.get("status")
        if target_status == "running" and source_status == "completed":
            status = "active"
        elif target_status == "completed" and source_status == "completed":
            status = "completed"
        elif target_status in {"failed", "interrupted"} and source_status == "completed":
            status = "failed"
        elif target_status == "blocked" or source_status in {"failed", "interrupted", "blocked"}:
            status = "blocked"
        elif source_status == "completed":
            status = "ready"
        else:
            status = "pending"
        edge["status"] = status

    total = len(nodes)
    completed = sum(node.get("status") == "completed" for node in nodes.values())
    active = [node["id"] for node in nodes.values() if node.get("status") == "running"]
    failed = [
        node["id"]
        for node in nodes.values()
        if node.get("status") in {"failed", "interrupted"}
    ]
    if active:
        current_team_id = str(nodes[active[0]].get("team_id") or "")
    elif failed:
        current_team_id = str(nodes[failed[0]].get("team_id") or "")
    elif completed == total:
        current_team_id = "decision"
    else:
        pending = next(
            (node for node in nodes.values() if node.get("status") in {"pending", "blocked"}),
            None,
        )
        current_team_id = str((pending or {}).get("team_id") or "context")
    group = next(
        (item for item in graph.get("groups", []) if item.get("id") == current_team_id),
        None,
    )
    current_stage = (group or {}).get("label") or "分析流程"
    if current_team_id == "research_team" and int(graph.get("schema_version") or 1) >= _SCHEMA_VERSION:
        debate = _research_debate(graph)
        speaker_id = debate.get("current_speaker_id")
        speaker = _NODE_BY_ID.get(str(speaker_id or ""), {})
        if debate.get("status") in {"pending", "running", "failed", "interrupted"}:
            current_stage = (
                f"Researcher Team · 第 {debate.get('current_round', 1)}/"
                f"{debate.get('max_rounds', _MAX_RESEARCH_DEBATE_ROUNDS)} 轮"
            )
            if speaker:
                current_stage += f" · {speaker['label']}"
    graph["progress"] = {
        "completed": completed,
        "total": total,
        "percent": round(completed * 100 / total) if total else 0,
        "active_node_ids": active,
        "failed_node_ids": failed,
        "current_team_id": current_team_id,
        "current_stage": current_stage,
    }


def _persist(graph: dict, callback: Callable[[dict], None]) -> None:
    graph["updated_at"] = _now_iso()
    _refresh_runtime(graph)
    callback(copy.deepcopy(graph))


def _clean_error(exc: Exception) -> str:
    return (str(exc).strip() or exc.__class__.__name__)[:500]


def _truncate(value: str) -> str:
    text = value.strip()
    if len(text) <= _MAX_NODE_OUTPUT:
        return text
    return f"{text[:_MAX_NODE_OUTPUT]}\n\n> 节点输出过长,已在 Graph 档案中截断。"


def _format_field_value(key: str, value):
    if key == "matched_strategies" and isinstance(value, list):
        return "、".join(
            str(item.get("name") or item.get("id") or "")
            for item in value
            if isinstance(item, dict)
        )
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _incoming_nodes(graph: dict, node_id: str) -> list[dict]:
    incoming_ids = [
        str(edge["source"])
        for edge in graph.get("edges", [])
        if edge.get("target") == node_id
        and edge.get("dependency", True)
        and _node(graph, str(edge["source"])).get("status") == "completed"
    ]
    return [_node(graph, incoming_id) for incoming_id in dict.fromkeys(incoming_ids)]


def _debate_prompt_history(graph: dict) -> list[dict]:
    debate = _research_debate(graph)
    return [
        {
            "round": turn.get("round"),
            "turn": turn.get("turn"),
            "speaker_id": turn.get("speaker_id"),
            "speaker_label": turn.get("speaker_label"),
            "argument": str((turn.get("output") or {}).get("markdown") or "")[
                :_MAX_UPSTREAM_PROMPT
            ],
        }
        for turn in _completed_debate_turns(debate)
    ]


def _structured_debate_context(graph: dict, node_id: str) -> dict | None:
    if node_id not in {*_RESEARCHER_NODE_IDS, "research_manager"}:
        return None
    debate = _research_debate(graph)
    history = _completed_debate_turns(debate)
    latest = history[-1] if history else None
    if node_id == "research_manager":
        round_number = int(debate.get("max_rounds") or _MAX_RESEARCH_DEBATE_ROUNDS)
        turn_number = len(history)
    else:
        position = _next_debate_position(debate)
        if position is None:
            round_number = int(debate.get("max_rounds") or _MAX_RESEARCH_DEBATE_ROUNDS)
            turn_number = len(history)
        else:
            turn_number, round_number, _ = position
    return {
        "debate_id": "research",
        "round": round_number,
        "turn": turn_number,
        "max_rounds": int(debate.get("max_rounds") or _MAX_RESEARCH_DEBATE_ROUNDS),
        "max_turns": int(debate.get("max_turns") or 2 * _MAX_RESEARCH_DEBATE_ROUNDS),
        "speaker_id": node_id,
        "previous_speaker_id": latest.get("speaker_id") if latest else None,
        "previous_argument_summary": (
            str((latest.get("output") or {}).get("summary") or "") if latest else ""
        ),
        "completed_turns": len(history),
    }


def _structured_input(
    graph: dict,
    node_id: str,
    analysis_input: StockAnalysisInput,
    target_context: dict,
    as_of: date,
) -> dict:
    definition = _NODE_BY_ID[node_id]
    fields = [
        {"key": "symbol", "label": "标的代码", "value": graph["symbol"]},
        {"key": "as_of", "label": "复盘日期", "value": as_of.isoformat()},
        {
            "key": "target_type",
            "label": "分析对象",
            "value": "策略候选" if graph["target_type"] == "candidate" else "冻结持仓",
        },
    ]
    fields.extend(
        {
            "key": key,
            "label": _FIELD_LABELS.get(key, key),
            "value": _format_field_value(key, value),
        }
        for key, value in target_context.items()
        if value is not None and value != "" and value != []
    )
    upstream = [
        {
            "node_id": item["id"],
            "label": item["label"],
            "status": item["status"],
            "summary": (
                item.get("output", {}).get("summary", "")
                if isinstance(item.get("output"), dict)
                else _plain_summary(str(item.get("output") or ""))
            ),
        }
        for item in _incoming_nodes(graph, node_id)
    ]
    result = {
        "summary": (
            f"{definition['label']} 将处理 {len(upstream)} 份上游材料。"
            if upstream
            else f"{definition['label']} 直接读取冻结的研究事实。"
        ),
        "facts_summary": analysis_input.summary,
        "fields": fields,
        "upstream": upstream,
    }
    debate_context = _structured_debate_context(graph, node_id)
    if debate_context is not None:
        result["debate"] = debate_context
        if node_id in _RESEARCHER_NODE_IDS:
            result["summary"] = (
                f"{definition['label']} 正在进行第 {debate_context['round']}/"
                f"{debate_context['max_rounds']} 轮辩论,此前已有 "
                f"{debate_context['completed_turns']} 次有效发言。"
            )
        else:
            result["summary"] = (
                f"{definition['label']} 将裁决 {debate_context['completed_turns']} 次"
                "完整多空辩论发言。"
            )
    return result


def _model_user_prompt(
    graph: dict,
    node_id: str,
    analysis_input: StockAnalysisInput,
) -> str:
    incoming = _incoming_nodes(graph, node_id)
    if node_id in _RESEARCHER_NODE_IDS:
        incoming = [item for item in incoming if item["id"] not in _RESEARCHER_NODE_IDS]
    upstream = {
        item["label"]: _node_markdown(item)[:_MAX_UPSTREAM_PROMPT]
        for item in incoming
    }
    debate_section = ""
    if node_id in _RESEARCHER_NODE_IDS:
        context = _structured_debate_context(graph, node_id) or {}
        history = _debate_prompt_history(graph)
        opponent = history[-1] if history else None
        debate_section = (
            "\n\n当前多空辩论状态:\n"
            f"- 当前是第 {context.get('round')}/{context.get('max_rounds')} 轮,"
            f"第 {context.get('turn')}/{context.get('max_turns')} 次发言。\n"
            f"- 当前发言方: {_NODE_BY_ID[node_id]['label']}。\n"
            "- 必须直接回应对方上一轮论点;若尚无对方发言,则建立首轮论证。\n"
            "对方上一轮观点:\n"
            f"```json\n{json.dumps(opponent, ensure_ascii=False)}\n```\n"
            "完整多空辩论记录:\n"
            f"```json\n{json.dumps(history, ensure_ascii=False)}\n```"
        )
    elif node_id == "research_manager":
        debate_section = (
            "\n\n完整多空辩论记录(按真实发言顺序):\n"
            f"```json\n{json.dumps(_debate_prompt_history(graph), ensure_ascii=False)}\n```\n"
            "必须裁决完整记录中的逐轮交锋,不能只读取双方最后一次摘要。"
        )
    return (
        f"{analysis_input.user_prompt}\n\n"
        "以下是本节点可以使用的上游研究材料:\n"
        f"```json\n{json.dumps(upstream, ensure_ascii=False)}\n```\n\n"
        "只使用上述事实和材料;上游为空或缺少某类数据时,明确写出未知项。"
        f"{debate_section}"
    )


async def _run_llm_node(
    graph: dict,
    node_id: str,
    *,
    analysis_input: StockAnalysisInput,
    target_context: dict,
    as_of: date,
    callback: Callable[[dict], None],
) -> None:
    node = _node(graph, node_id)
    node.update(
        status="running",
        attempt=int(node.get("attempt") or 0) + 1,
        input=_structured_input(graph, node_id, analysis_input, target_context, as_of),
        error=None,
        started_at=_now_iso(),
        completed_at=None,
    )
    _add_event(graph, node_id, "running", f"开始执行 {node['label']}")
    _persist(graph, callback)
    try:
        output = await generate_ai_text(
            [
                {
                    "role": "system",
                    "content": _system_prompt(node_id),
                },
                {
                    "role": "user",
                    "content": _model_user_prompt(graph, node_id, analysis_input),
                },
            ],
            temperature=0.1 if node_id in {"research_manager", "portfolio_manager"} else 0.2,
            max_tokens=2200 if node_id == "portfolio_manager" else 1500,
        )
        if not output.strip():
            raise RuntimeError("Agent 未生成有效内容")
        structured = _validated_model_output(node_id, output)
        node.update(
            status="completed",
            output=structured,
            error=None,
            completed_at=_now_iso(),
        )
        _add_event(
            graph,
            node_id,
            "completed",
            structured["summary"] or f"{node['label']} 已完成",
        )
    except Exception as exc:
        node.update(status="failed", error=_clean_error(exc), completed_at=_now_iso())
        _add_event(graph, node_id, "failed", node["error"])
    _persist(graph, callback)


async def _run_research_debate_turn(
    graph: dict,
    *,
    turn_number: int,
    round_number: int,
    speaker_id: str,
    analysis_input: StockAnalysisInput,
    target_context: dict,
    as_of: date,
    callback: Callable[[dict], None],
) -> bool:
    debate = _research_debate(graph)
    node = _node(graph, speaker_id)
    started_at = _now_iso()
    node.update(
        status="running",
        attempt=int(node.get("attempt") or 0) + 1,
        input=_structured_input(graph, speaker_id, analysis_input, target_context, as_of),
        error=None,
        started_at=started_at,
        completed_at=None,
    )
    debate.update(
        status="running",
        current_round=round_number,
        current_speaker_id=speaker_id,
        error=None,
        started_at=debate.get("started_at") or started_at,
        completed_at=None,
    )
    record = {
        "id": (
            f"{graph['id']}:research:turn-{turn_number}:"
            f"attempt-{int(node['attempt'])}"
        ),
        "round": round_number,
        "turn": turn_number,
        "speaker_id": speaker_id,
        "speaker_label": node["label"],
        "status": "running",
        "attempt": int(node["attempt"]),
        "input": copy.deepcopy((node.get("input") or {}).get("debate")),
        "output": None,
        "error": None,
        "started_at": started_at,
        "completed_at": None,
    }
    debate.setdefault("history", []).append(record)
    _add_event(
        graph,
        speaker_id,
        "running",
        f"第 {round_number}/{debate['max_rounds']} 轮 · {node['label']} 开始发言",
    )
    _persist(graph, callback)
    try:
        output = await generate_ai_text(
            [
                {"role": "system", "content": _system_prompt(speaker_id)},
                {
                    "role": "user",
                    "content": _model_user_prompt(graph, speaker_id, analysis_input),
                },
            ],
            temperature=0.2,
            max_tokens=1500,
        )
        if not output.strip():
            raise RuntimeError("Agent 未生成有效内容")
        structured = _validated_model_output(speaker_id, output)
        completed_at = _now_iso()
        record.update(
            status="completed",
            output=structured,
            error=None,
            completed_at=completed_at,
        )
        node.update(
            status="completed",
            output=structured,
            error=None,
            completed_at=completed_at,
        )
        debate["completed_turns"] = len(_completed_debate_turns(debate))
        next_position = _next_debate_position(debate)
        if next_position is None:
            debate.update(
                status="completed",
                current_round=int(debate["max_rounds"]),
                current_speaker_id=None,
                error=None,
                completed_at=completed_at,
            )
            for researcher_id in _RESEARCHER_NODE_IDS:
                _node(graph, researcher_id)["status"] = "completed"
        else:
            _, next_round, next_speaker_id = next_position
            debate.update(
                status="running",
                current_round=next_round,
                current_speaker_id=next_speaker_id,
            )
            next_node = _node(graph, next_speaker_id)
            next_node.update(
                status="pending",
                error=None,
                started_at=None,
                completed_at=None,
            )
        _add_event(
            graph,
            speaker_id,
            "completed",
            (
                f"第 {round_number}/{debate['max_rounds']} 轮 · {node['label']} 完成发言: "
                f"{structured['summary'] or '已形成辩论观点'}"
            ),
        )
    except Exception as exc:
        error = _clean_error(exc)
        completed_at = _now_iso()
        record.update(status="failed", error=error, completed_at=completed_at)
        node.update(status="failed", error=error, completed_at=completed_at)
        debate.update(
            status="failed",
            completed_turns=len(_completed_debate_turns(debate)),
            current_round=round_number,
            current_speaker_id=speaker_id,
            error=error,
            completed_at=completed_at,
        )
        _add_event(
            graph,
            speaker_id,
            "failed",
            f"第 {round_number}/{debate['max_rounds']} 轮发言失败: {error}",
        )
    _persist(graph, callback)
    return record["status"] == "completed"


async def _run_research_debate(
    graph: dict,
    *,
    analysis_input: StockAnalysisInput,
    target_context: dict,
    as_of: date,
    callback: Callable[[dict], None],
) -> bool:
    debate = _research_debate(graph)
    if debate.get("status") == "completed" and _next_debate_position(debate) is None:
        return True
    while (position := _next_debate_position(debate)) is not None:
        turn_number, round_number, speaker_id = position
        speaker = _node(graph, speaker_id)
        if speaker.get("status") not in {"pending", "completed"}:
            return False
        if not await _run_research_debate_turn(
            graph,
            turn_number=turn_number,
            round_number=round_number,
            speaker_id=speaker_id,
            analysis_input=analysis_input,
            target_context=target_context,
            as_of=as_of,
            callback=callback,
        ):
            return False
    return debate.get("status") == "completed"


def _dependency_sources(graph: dict, node_id: str) -> list[str]:
    return [
        str(edge["source"])
        for edge in graph.get("edges", [])
        if edge.get("target") == node_id and edge.get("dependency", True)
    ]


def _downstream(graph: dict, node_id: str) -> set[str]:
    pending = [node_id]
    result: set[str] = set()
    while pending:
        source = pending.pop()
        for edge in graph.get("edges", []):
            if edge.get("dependency", True) is False:
                continue
            if edge.get("source") == source and edge.get("target") not in result:
                target = str(edge["target"])
                result.add(target)
                pending.append(target)
    result.discard(node_id)
    return result


def _block_descendants(graph: dict, node_id: str) -> None:
    for downstream_id in _downstream(graph, node_id):
        downstream = _node(graph, downstream_id)
        if downstream.get("status") not in {"completed", "failed", "interrupted"}:
            downstream["status"] = "blocked"


def _dependencies_completed(graph: dict, node_id: str) -> bool:
    return all(
        _node(graph, source_id).get("status") == "completed"
        for source_id in _dependency_sources(graph, node_id)
    )


async def run_graph(
    graph: dict,
    *,
    repo,
    data_dir: Path,
    target_context: dict,
    as_of: date,
    on_update: Callable[[dict], None],
) -> dict:
    """执行待处理节点;已完成节点作为 checkpoint 直接复用。"""
    if needs_upgrade(graph):
        raise RuntimeError("旧版分析 Graph 需要先升级后再执行")
    graph["status"] = "running"
    _persist(graph, on_update)

    facts = _node(graph, "facts")
    pending_model_nodes = [
        node["id"]
        for node in graph["nodes"]
        if node["id"] != "facts" and node.get("status") == "pending"
    ]
    analysis_input: StockAnalysisInput | None = None
    if facts.get("status") != "completed" or pending_model_nodes:
        if facts.get("status") != "completed":
            facts.update(
                status="running",
                attempt=int(facts.get("attempt") or 0) + 1,
                error=None,
                started_at=_now_iso(),
                completed_at=None,
            )
            _add_event(graph, "facts", "running", "开始装配冻结研究事实")
            _persist(graph, on_update)
        try:
            analysis_input = await asyncio.to_thread(
                build_stock_analysis_input,
                repo,
                data_dir,
                graph["symbol"],
                "每日复盘 TradingAgents 风格客观研究",
                target_context,
                as_of,
            )
            graph["artifacts"] = {
                "summary": analysis_input.summary,
                "levels": analysis_input.levels,
                "close": analysis_input.close,
                "news_evidence": copy.deepcopy(target_context.get("news_evidence") or []),
            }
            if facts.get("status") != "completed":
                facts_markdown = (
                    f"### 已装配事实\n\n{graph['symbol']} 截至 {as_of.isoformat()} 的行情、"
                    "指标、关键价位、新闻证据与目标身份已经冻结。"
                )
                facts.update(
                    status="completed",
                    input=_structured_input(
                        graph,
                        "facts",
                        analysis_input,
                        target_context,
                        as_of,
                    ),
                    output=_structured_output(facts_markdown),
                    error=None,
                    completed_at=_now_iso(),
                )
                _add_event(graph, "facts", "completed", "研究事实已冻结并可追溯")
        except Exception as exc:
            error = _clean_error(exc)
            failed_node = facts if facts.get("status") != "completed" else _node(
                graph,
                pending_model_nodes[0],
            )
            failed_node.update(status="failed", error=error, completed_at=_now_iso())
            _add_event(graph, failed_node["id"], "failed", error)
            _block_descendants(graph, failed_node["id"])
            graph["status"] = "failed"
            _persist(graph, on_update)
            return graph
        _persist(graph, on_update)

    if analysis_input is None and pending_model_nodes:
        raise RuntimeError("执行待处理节点时缺少研究输入")

    pending_analysts = [
        node_id
        for node_id in _ANALYST_NODE_IDS
        if _node(graph, node_id).get("status") == "pending"
    ]
    if pending_analysts:
        await asyncio.gather(*(
            _run_llm_node(
                graph,
                node_id,
                analysis_input=analysis_input,
                target_context=target_context,
                as_of=as_of,
                callback=on_update,
            )
            for node_id in pending_analysts
        ))

    failed_analysts = [
        node_id
        for node_id in _ANALYST_NODE_IDS
        if _node(graph, node_id).get("status") != "completed"
    ]
    if failed_analysts:
        for node_id in failed_analysts:
            _block_descendants(graph, node_id)
        graph["status"] = "failed"
        _persist(graph, on_update)
        return graph

    debate = _research_debate(graph)
    if debate.get("status") != "completed" or _next_debate_position(debate) is not None:
        debate_completed = await _run_research_debate(
            graph,
            analysis_input=analysis_input,
            target_context=target_context,
            as_of=as_of,
            callback=on_update,
        )
        if not debate_completed:
            failed_speaker_id = str(debate.get("current_speaker_id") or "bull_researcher")
            _block_descendants(graph, failed_speaker_id)
            graph["status"] = "failed"
            _persist(graph, on_update)
            return graph

    for node_id in _SEQUENTIAL_NODE_IDS:
        node = _node(graph, node_id)
        if node.get("status") == "completed":
            continue
        if node.get("status") != "pending" or not _dependencies_completed(graph, node_id):
            if node.get("status") not in {"failed", "interrupted"}:
                node["status"] = "blocked"
            graph["status"] = "failed"
            _persist(graph, on_update)
            return graph
        await _run_llm_node(
            graph,
            node_id,
            analysis_input=analysis_input,
            target_context=target_context,
            as_of=as_of,
            callback=on_update,
        )
        if node.get("status") != "completed":
            _block_descendants(graph, node_id)
            graph["status"] = "failed"
            _persist(graph, on_update)
            return graph

    graph["status"] = "completed"
    _persist(graph, on_update)
    return graph


def prepare_node_retry(graph: dict, node_id: str) -> bool:
    """恢复指定失败节点及依赖下游,保留不受影响的 checkpoint。"""
    selected = _node(graph, node_id)
    if selected.get("status") not in {"failed", "interrupted"}:
        return False
    if node_id in _RESEARCHER_NODE_IDS:
        if not _prepare_research_debate_retry(graph, node_id):
            return False
        reset_ids: set[str] = set()
    else:
        reset_ids = {node_id, *_downstream(graph, node_id)}
    for reset_id in reset_ids:
        _reset_node_checkpoint(_node(graph, reset_id))
    if node_id == "facts":
        graph["artifacts"] = {}
    graph["status"] = "pending"
    _add_event(graph, node_id, "pending", f"已从 {selected['label']} 创建恢复 checkpoint")
    graph["updated_at"] = _now_iso()
    _refresh_runtime(graph)
    return True


def _reset_node_checkpoint(node: dict, *, output: dict | None = None) -> None:
    node.update(
        status="pending",
        input=None,
        output=copy.deepcopy(output),
        error=None,
        started_at=None,
        completed_at=None,
    )


def _prepare_research_debate_retry(graph: dict, speaker_id: str) -> bool:
    debate = _research_debate(graph)
    position = _next_debate_position(debate)
    if position is None or position[2] != speaker_id:
        return False

    for researcher_id in _RESEARCHER_NODE_IDS:
        researcher = _node(graph, researcher_id)
        latest = _latest_completed_turn(debate, researcher_id)
        latest_output = (latest or {}).get("output")
        if researcher_id == speaker_id:
            _reset_node_checkpoint(researcher, output=latest_output)
        elif latest is not None:
            researcher.update(
                status="completed",
                input=researcher.get("input"),
                output=copy.deepcopy(latest_output),
                error=None,
                started_at=latest.get("started_at"),
                completed_at=latest.get("completed_at"),
            )
        else:
            researcher.update(status="blocked", error=None)

    reset_ids = {"research_manager", *_downstream(graph, "research_manager")}
    for reset_id in reset_ids:
        _reset_node_checkpoint(_node(graph, reset_id))
    _, round_number, _ = position
    debate.update(
        status="pending",
        completed_turns=len(_completed_debate_turns(debate)),
        current_round=round_number,
        current_speaker_id=speaker_id,
        error=None,
        completed_at=None,
    )
    return True


def prepare_failed_retry(graph: dict) -> bool:
    """恢复 Graph 中全部失败或中断根节点。"""
    failed_ids = [
        node["id"]
        for node in graph.get("nodes", [])
        if node.get("status") in {"failed", "interrupted"}
    ]
    if not failed_ids:
        return False
    debate = _research_debate(graph)
    debate_speaker_id = str(debate.get("current_speaker_id") or "")
    debate_recovered = (
        debate_speaker_id in _RESEARCHER_NODE_IDS
        and debate_speaker_id in failed_ids
        and _prepare_research_debate_retry(graph, debate_speaker_id)
    )
    generic_failed_ids = [
        node_id
        for node_id in failed_ids
        if not (debate_recovered and node_id in _RESEARCHER_NODE_IDS)
    ]
    reset_ids = set(generic_failed_ids)
    for node_id in generic_failed_ids:
        reset_ids.update(_downstream(graph, node_id))
    for node_id in reset_ids:
        _reset_node_checkpoint(_node(graph, node_id))
    if "facts" in reset_ids:
        graph["artifacts"] = {}
    graph["status"] = "pending"
    for node_id in failed_ids:
        _add_event(graph, node_id, "pending", f"已恢复 {_node(graph, node_id)['label']}")
    graph["updated_at"] = _now_iso()
    _refresh_runtime(graph)
    return True


def mark_interrupted(graph: dict) -> bool:
    changed = False
    for node in graph.get("nodes", []):
        if node.get("status") in {"pending", "running"}:
            node.update(status="interrupted", error="服务重启前节点未完成")
            _add_event(graph, node["id"], "interrupted", node["error"])
            changed = True
    if changed and int(graph.get("schema_version") or 1) >= _SCHEMA_VERSION:
        debate = _research_debate(graph)
        running_turn = next(
            (
                turn
                for turn in reversed(debate.get("history", []))
                if turn.get("status") == "running"
            ),
            None,
        )
        if running_turn is not None:
            running_turn.update(
                status="interrupted",
                error="服务重启前辩论发言未完成",
                completed_at=_now_iso(),
            )
        if debate.get("status") in {"pending", "running"}:
            debate.update(
                status="interrupted",
                error="服务重启前多空辩论未完成",
                completed_at=_now_iso(),
            )
    if changed:
        graph["status"] = "interrupted"
        graph["updated_at"] = _now_iso()
        _refresh_runtime(graph)
    return changed


def graph_error(graph: dict) -> str | None:
    failed = next(
        (
            node
            for node in graph.get("nodes", [])
            if node.get("status") in {"failed", "interrupted"}
        ),
        None,
    )
    if failed is None:
        return None
    return failed.get("error") or f"{failed.get('label', '分析节点')}未完成"


def graph_report(graph: dict) -> dict:
    manager = _node(graph, "portfolio_manager")
    markdown = _node_markdown(manager)
    if manager.get("status") != "completed" or not markdown:
        raise RuntimeError("TradingAgents 研究 Graph 尚未生成最终研究结论")
    if not re.match(r"^#\s+", markdown.lstrip()):
        markdown = f"# {graph['symbol']} TradingAgents 研究结论\n\n{markdown}"
    artifacts = graph.get("artifacts") or {}
    return {
        "content": markdown,
        "summary": artifacts.get("summary", ""),
        "close": artifacts.get("close"),
        "levels": artifacts.get("levels"),
    }
