"""内置扩展预设的删除并发边界测试。"""
from __future__ import annotations

import pytest

from app.services import ext_presets
from app.services.ext_data import ExtConfigChangedError, ExtConfigStore, PullConfig


@pytest.mark.asyncio
async def test_deleted_preset_rejects_inflight_fetch_write(tmp_path, monkeypatch) -> None:
    await ext_presets.ensure_builtin_presets(tmp_path)
    store = ExtConfigStore(tmp_path)
    assert store.get("ext_gn_ths") is not None

    async def delete_then_return(_url: str) -> list[dict]:
        assert store.delete("ext_gn_ths") is True
        return [{
            "symbol": "600000.SH",
            "name": "浦发银行",
            "concepts": ["银行"],
        }]

    monkeypatch.setattr(ext_presets, "_fetch_json", delete_then_return)
    with pytest.raises(ExtConfigChangedError, match="已删除"):
        await ext_presets.fetch_preset("ext_gn_ths", tmp_path)

    assert not (tmp_path / "ext_data" / "ext_gn_ths").exists()


@pytest.mark.asyncio
async def test_preset_fetch_keeps_builtin_definition(tmp_path, monkeypatch) -> None:
    await ext_presets.ensure_builtin_presets(tmp_path)
    store = ExtConfigStore(tmp_path)
    customized = store.get("ext_gn_ths")
    assert customized is not None
    customized.pull = PullConfig(url="https://user.invalid/custom")
    store.update(customized)
    requested: list[str] = []

    async def capture_url(url: str) -> list[dict]:
        requested.append(url)
        return [{
            "symbol": "600000.SH",
            "name": "浦发银行",
            "concepts": ["银行"],
        }]

    monkeypatch.setattr(ext_presets, "_fetch_json", capture_url)
    assert await ext_presets.fetch_preset("ext_gn_ths", tmp_path) == 1

    assert requested == [ext_presets._CONCEPT_DATA_URL]
    assert store.get("ext_gn_ths").pull.url == "https://user.invalid/custom"
