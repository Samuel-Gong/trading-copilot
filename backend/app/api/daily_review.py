"""每日复盘档案 API。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.services import daily_analysis_graph, daily_review

router = APIRouter(prefix="/api/daily-review", tags=["daily-review"])


class GraphNodeRetryRequest(BaseModel):
    target_type: str
    source_ref: str
    node_id: str


class DailyReviewRunRequest(BaseModel):
    strategy_ids: list[str] = Field(default_factory=list)


@router.get("/graph-definition")
def get_graph_definition():
    return daily_analysis_graph.graph_definition()


@router.get("/routines")
def list_routines():
    items = daily_review.list_routines()
    return {
        "items": items,
        "running_count": sum(item["status"] == "running" for item in items),
    }


@router.post("/routines/interrupt-all")
async def interrupt_all_routines():
    routine_ids = await daily_review.interrupt_all()
    return {"interrupted_count": len(routine_ids), "routine_ids": routine_ids}


@router.get("/routines/{routine_ref}")
def get_routine(routine_ref: str):
    return {"routine": daily_review.get_routine(routine_ref)}


@router.post("/routines/{business_date}/run")
def run_routine(
    business_date: date,
    request: Request,
    background_tasks: BackgroundTasks,
    body: DailyReviewRunRequest | None = None,
):
    routine, created = daily_review.prepare_routine(
        request.app.state.repo,
        business_date,
        strategy_ids=body.strategy_ids if body is not None else [],
    )
    if created:
        background_tasks.add_task(
            daily_review.run_routine,
            request.app.state,
            routine["id"],
        )
        return JSONResponse(status_code=202, content=routine)
    return routine


@router.post("/routines/{routine_ref}/retry")
def retry_routine(routine_ref: str, request: Request, background_tasks: BackgroundTasks):
    routine, started = daily_review.prepare_retry(routine_ref)
    if routine is None:
        raise HTTPException(status_code=404, detail="每日复盘档案不存在")
    if started:
        background_tasks.add_task(
            daily_review.run_routine,
            request.app.state,
            routine["id"],
        )
        return JSONResponse(status_code=202, content=routine)
    return routine


@router.post("/routines/{routine_ref}/graph/retry")
def retry_graph_node(
    routine_ref: str,
    body: GraphNodeRetryRequest,
    request: Request,
    background_tasks: BackgroundTasks,
):
    routine, started, error = daily_review.prepare_node_retry(
        routine_ref,
        target_type=body.target_type,
        source_ref=body.source_ref,
        node_id=body.node_id,
    )
    if routine is None:
        raise HTTPException(status_code=404, detail=error or "每日复盘档案不存在")
    if not started:
        status_code = 404 if error == "分析目标不存在" else 409
        raise HTTPException(status_code=status_code, detail=error or "分析节点无法恢复")
    background_tasks.add_task(
        daily_review.run_routine,
        request.app.state,
        routine["id"],
    )
    return JSONResponse(status_code=202, content=routine)
