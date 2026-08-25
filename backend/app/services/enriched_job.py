"""会推进 enriched generation 的长任务执行边界。"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run_enriched_job_with_repository_refresh(
    repo,
    operation,
    quote_service=None,
):
    """暂停实时写入执行 enriched 任务，并恢复已发布 generation 的快照。"""
    def generation() -> str | None:
        try:
            return repo.get_matrix_data_generation("stock")
        except Exception:  # noqa: BLE001
            logger.warning("read enriched generation failed", exc_info=True)
            return None

    def refresh_after_partial_publication(before: str | None) -> None:
        after = generation()
        if after is not None and after != before:
            repo.refresh_cache()

    def run():
        before = generation()
        try:
            result = operation()
        except Exception:  # noqa: BLE001
            try:
                refresh_after_partial_publication(before)
            except Exception:  # noqa: BLE001
                # 保留原任务异常；刷新失败会记录，任务仍由上层标记为 failed。
                logger.exception("refresh repository after partial publication failed")
            raise

        after = generation()
        succeeded = not isinstance(result, dict) or "error" not in result
        if succeeded or (after is not None and after != before):
            repo.refresh_cache()
        return result

    if quote_service is None:
        return run()
    with quote_service.paused():
        return run()
