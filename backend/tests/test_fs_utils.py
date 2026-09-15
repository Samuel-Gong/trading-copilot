from __future__ import annotations

from pathlib import Path

import pytest

from app.services import fs_utils


def test_atomic_write_text_replace_failure_preserves_old_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "definition.json"
    target.write_text("old", encoding="utf-8")
    real_replace = fs_utils.os.replace

    def fail_target_replace(source, destination):
        if Path(destination) == target:
            raise OSError("synthetic replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(fs_utils.os, "replace", fail_target_replace)

    with pytest.raises(OSError, match="synthetic replace failure"):
        fs_utils.atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".definition.json.*.tmp")) == []
