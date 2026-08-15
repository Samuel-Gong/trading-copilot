"""API 路由 — Phase 0 仅 /health 与 /api/capabilities。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app import __version__
from app.config import settings
from app.tickflow import client as tf_client
from app.tickflow.policy import detect_capabilities, tier_label

router = APIRouter()


@router.get("/health")
def health() -> dict:
    payload = {
        "status": "ok",
        "version": __version__,
        # 三态: none(无key/无效) / free(免费key) / api_key(付费档)
        "mode": tf_client.current_mode(),
    }
    if settings.git_sha:
        payload["git_sha"] = settings.git_sha
    if settings.build_time:
        payload["build_time"] = settings.build_time
    return payload


@router.get("/api/capabilities")
def capabilities() -> dict:
    """前端用来决定哪些功能可用、哪些灰显。"""
    capset = detect_capabilities()
    return {
        "label": tier_label(),
        "capabilities": capset.to_dict(),
    }


@router.post("/api/capabilities/redetect")
def redetect(request: Request) -> dict:
    """用户在设置页"重新检测"按钮。"""
    capset = detect_capabilities(force=True)
    request.app.state.capabilities = capset
    financial_scheduler = getattr(request.app.state, "financial_scheduler", None)
    if financial_scheduler:
        financial_scheduler.update_capabilities(capset)
    quote_service = getattr(request.app.state, "quote_service", None)
    if quote_service:
        quote_service.boot_check()
    return {
        "label": tier_label(),
        "capabilities": capset.to_dict(),
    }
