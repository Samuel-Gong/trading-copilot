"""市场主线过滤设置的 API 边界测试。"""

import pytest
from fastapi import HTTPException

from app.api import settings as settings_api
from app.services import preferences


def test_mainline_filter_rejects_unversioned_st_toggle(tmp_path, monkeypatch):
    """没有 point-in-time 风险警示数据时，不得保存一个无实际作用的 ST 开关。"""
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")

    with pytest.raises(HTTPException) as exc_info:
        settings_api.update_mainline_filter(
            settings_api.MainlineFilterIn(exclude_st=False),
        )

    assert exc_info.value.status_code == 409
    assert "point-in-time" in str(exc_info.value.detail)
    assert not (tmp_path / "preferences.json").exists()


def test_mainline_filter_rejects_min_members_above_max_members(tmp_path, monkeypatch):
    """矛盾的成员数区间不得落盘，避免重算后清空全部主线。"""
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")

    with pytest.raises(HTTPException) as exc_info:
        settings_api.update_mainline_filter(
            settings_api.MainlineFilterIn(min_members=200, max_members=50),
        )

    assert exc_info.value.status_code == 422
    assert "下限不能大于上限" in str(exc_info.value.detail)
    assert not (tmp_path / "preferences.json").exists()
