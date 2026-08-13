"""每日复盘研究 Agent 的 Pydantic structured output 契约。"""
from __future__ import annotations

import json
import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ResearchText = Annotated[str, Field(min_length=1, max_length=800)]
ResearchStatus = Literal[
    "支持性证据占优",
    "反证占优",
    "证据均衡",
    "证据不足",
    "数据冲突",
]
EvidenceGrade = Literal["高", "中", "低"]

_FORBIDDEN_ACTION_PATTERN = re.compile(
    r"买入|卖出|加仓|减仓|建仓|平仓|清仓|调仓|止损|止盈|仓位|目标价|"
    r"交易指令|操作建议|\bbuy\b|\bsell\b|\bhold\b|overweight|underweight|"
    r"entry\s+price|stop\s+loss|position\s+sizing|price\s+target",
    re.IGNORECASE,
)


class ResearchOnlyModel(BaseModel):
    """所有研究输出的共同红线。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="after")
    def reject_trading_actions(self) -> Self:
        content = json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
        matched = _FORBIDDEN_ACTION_PATTERN.search(content)
        if matched:
            raise ValueError(f"研究输出包含被禁止的交易执行内容: {matched.group(0)}")
        return self


class EvidenceAssessment(ResearchOnlyModel):
    """研究经理对单项证据的分级判断。"""

    conclusion: ResearchText = Field(description="需要评级的共识或分歧结论")
    grade: EvidenceGrade = Field(description="证据等级,只能是高、中或低")
    basis: ResearchText = Field(description="评级所依据的冻结事实或上游研究材料")


class BullResearchResult(ResearchOnlyModel):
    """Bull Researcher 的支持性研究论证。"""

    supporting_viewpoints: list[ResearchText] = Field(
        min_length=1,
        max_length=8,
        description="支持性观点,聚焦增长潜力、竞争优势和积极指标",
    )
    bear_response: list[ResearchText] = Field(
        min_length=1,
        max_length=8,
        description="对 Bear 上一轮反证的直接回应;首轮说明尚无对方观点",
    )
    evidence_chain: list[ResearchText] = Field(
        min_length=1,
        max_length=8,
        description="从冻结事实到支持性结论的可追溯证据链",
    )
    conditions: list[ResearchText] = Field(
        min_length=1,
        max_length=8,
        description="各项支持性论证继续成立所需的前提",
    )


class BearResearchResult(ResearchOnlyModel):
    """Bear Researcher 的反方研究论证。"""

    counter_evidence: list[ResearchText] = Field(
        min_length=1,
        max_length=8,
        description="反方证据,聚焦风险、挑战、竞争弱点和消极指标",
    )
    bull_response: list[ResearchText] = Field(
        min_length=1,
        max_length=8,
        description="对 Bull 上一轮论点的直接质询与回应",
    )
    fragile_assumptions: list[ResearchText] = Field(
        min_length=1,
        max_length=8,
        description="支持性论证中最脆弱的假设",
    )
    invalidation_conditions: list[ResearchText] = Field(
        min_length=1,
        max_length=8,
        description="会使支持性论证失效的可验证条件",
    )


class ResearchManagerResult(ResearchOnlyModel):
    """Research Manager 对完整辩论的研究裁决。"""

    confirmed_consensus: list[ResearchText] = Field(min_length=1, max_length=8)
    unresolved_disagreements: list[ResearchText] = Field(min_length=1, max_length=8)
    evidence_assessment: list[EvidenceAssessment] = Field(min_length=1, max_length=10)
    research_ruling: ResearchText = Field(description="基于证据形成的明确研究裁决")
    research_status: ResearchStatus = Field(description="研究状态量表中的单一结果")


class TraderResearchPlan(ResearchOnlyModel):
    """Trader 在红线内形成的非交易性后续研究方案。"""

    research_status: ResearchStatus
    observation_items: list[ResearchText] = Field(min_length=1, max_length=10)
    upgrade_or_invalidation_facts: list[ResearchText] = Field(min_length=1, max_length=10)


class AggressiveRiskReview(ResearchOnlyModel):
    """Aggressive Analyst 的积极风险视角审查。"""

    support_factors: list[ResearchText] = Field(min_length=1, max_length=8)
    required_conditions: list[ResearchText] = Field(min_length=1, max_length=8)
    amplified_risks: list[ResearchText] = Field(min_length=1, max_length=8)


class ConservativeRiskReview(ResearchOnlyModel):
    """Conservative Analyst 的资本保护视角审查。"""

    tail_risks: list[ResearchText] = Field(min_length=1, max_length=8)
    weakest_evidence: list[ResearchText] = Field(min_length=1, max_length=8)
    unacceptable_data_gaps: list[ResearchText] = Field(min_length=1, max_length=8)


class NeutralRiskReview(ResearchOnlyModel):
    """Neutral Analyst 的平衡风险视角审查。"""

    common_risks: list[ResearchText] = Field(min_length=1, max_length=8)
    disagreement_risks: list[ResearchText] = Field(min_length=1, max_length=8)
    minimum_observation_set: list[ResearchText] = Field(min_length=1, max_length=8)


class PortfolioResearchDecision(ResearchOnlyModel):
    """Portfolio Manager 的最终研究结论。"""

    overall_conclusion: ResearchText
    evidence_consensus: list[ResearchText] = Field(min_length=1, max_length=10)
    key_disagreements: list[ResearchText] = Field(min_length=1, max_length=10)
    risk_boundary: list[ResearchText] = Field(min_length=1, max_length=10)
    facts_to_watch: list[ResearchText] = Field(min_length=1, max_length=10)
    research_status: ResearchStatus


StructuredResearchModel = (
    BullResearchResult
    | BearResearchResult
    | ResearchManagerResult
    | TraderResearchPlan
    | AggressiveRiskReview
    | ConservativeRiskReview
    | NeutralRiskReview
    | PortfolioResearchDecision
)

_STRUCTURED_MODELS: dict[str, type[ResearchOnlyModel]] = {
    "bull_researcher": BullResearchResult,
    "bear_researcher": BearResearchResult,
    "research_manager": ResearchManagerResult,
    "trader": TraderResearchPlan,
    "aggressive_risk": AggressiveRiskReview,
    "conservative_risk": ConservativeRiskReview,
    "neutral_risk": NeutralRiskReview,
    "portfolio_manager": PortfolioResearchDecision,
}


def structured_model_for(node_id: str) -> type[ResearchOnlyModel] | None:
    """返回节点对应的 Pydantic 输出模型。"""
    return _STRUCTURED_MODELS.get(node_id)


def response_format_for(node_id: str) -> dict | None:
    """返回设置页可展示、运行时可复用的 structured output 契约。"""
    model = structured_model_for(node_id)
    if model is None:
        return None
    return {
        "type": "pydantic",
        "model": model.__name__,
        "json_schema": model.model_json_schema(),
    }


def structured_output_instruction(node_id: str) -> str:
    """把 Pydantic 契约注入系统提示词。"""
    response_format = response_format_for(node_id)
    if response_format is None:
        return "使用简洁中文 Markdown,并用清晰标题组织内容。"
    schema = json.dumps(
        response_format["json_schema"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"输出由 Pydantic 模型 {response_format['model']} 强校验。"
        "只返回一个符合下方 JSON Schema 的 JSON 对象,不要使用 Markdown 代码块,"
        "不要增加 schema 之外的字段。所有字符串使用中文。\n"
        f"JSON Schema: {schema}"
    )


def parse_structured_output(node_id: str, raw_output: str) -> StructuredResearchModel:
    """校验模型原始输出;任何格式、字段或红线错误都 fail closed。"""
    model = structured_model_for(node_id)
    if model is None:
        raise ValueError(f"节点 {node_id} 没有 structured output 契约")
    text = raw_output.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    return model.model_validate_json(text)


def _render_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _render_sections(sections: list[tuple[str, str | list[str]]]) -> str:
    blocks = []
    for title, value in sections:
        content = _render_list(value) if isinstance(value, list) else value
        blocks.append(f"### {title}\n\n{content}")
    return "\n\n".join(blocks)


def render_structured_output(output: StructuredResearchModel) -> str:
    """把已通过 Pydantic 校验的数据渲染成现有 UI 使用的中文 Markdown。"""
    if isinstance(output, BullResearchResult):
        sections = [
            ("支持观点", output.supporting_viewpoints),
            ("对 Bear 的回应", output.bear_response),
            ("证据链", output.evidence_chain),
            ("成立前提", output.conditions),
        ]
    elif isinstance(output, BearResearchResult):
        sections = [
            ("主要反证", output.counter_evidence),
            ("对 Bull 的回应", output.bull_response),
            ("脆弱假设", output.fragile_assumptions),
            ("论证失效条件", output.invalidation_conditions),
        ]
    elif isinstance(output, ResearchManagerResult):
        assessments = [
            f"{item.conclusion}(证据等级:{item.grade};依据:{item.basis})"
            for item in output.evidence_assessment
        ]
        sections = [
            ("已确认共识", output.confirmed_consensus),
            ("未解决分歧", output.unresolved_disagreements),
            ("证据等级", assessments),
            ("研究裁决", output.research_ruling),
            ("研究状态", output.research_status),
        ]
    elif isinstance(output, TraderResearchPlan):
        sections = [
            ("研究状态", output.research_status),
            ("后续观察清单", output.observation_items),
            ("结论升级或失效所需事实", output.upgrade_or_invalidation_facts),
        ]
    elif isinstance(output, AggressiveRiskReview):
        sections = [
            ("可支持因素", output.support_factors),
            ("必要条件", output.required_conditions),
            ("放大风险", output.amplified_risks),
        ]
    elif isinstance(output, ConservativeRiskReview):
        sections = [
            ("尾部风险", output.tail_risks),
            ("最弱证据", output.weakest_evidence),
            ("不可接受的数据缺口", output.unacceptable_data_gaps),
        ]
    elif isinstance(output, NeutralRiskReview):
        sections = [
            ("共同风险", output.common_risks),
            ("分歧风险", output.disagreement_risks),
            ("最小后续观察集", output.minimum_observation_set),
        ]
    else:
        sections = [
            ("综合结论", output.overall_conclusion),
            ("证据共识", output.evidence_consensus),
            ("关键分歧", output.key_disagreements),
            ("风险边界", output.risk_boundary),
            ("仍需观察的事实", output.facts_to_watch),
            ("研究状态", output.research_status),
        ]
    return _render_sections(sections)
