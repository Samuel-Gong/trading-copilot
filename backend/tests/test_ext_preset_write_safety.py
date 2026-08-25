"""内置扩展预设的删除并发边界测试。"""
from __future__ import annotations

import pytest

from app.services import ext_presets
from app.services.ext_data import ExtConfigChangedError, ExtConfigStore


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
