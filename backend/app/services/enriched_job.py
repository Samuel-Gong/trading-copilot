"""会推进 enriched generation 的长任务执行边界。"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_REPOSITORY_REFRESH_WAIT_SECONDS = 30.0
_REPOSITORY_REFRESH_POLL_SECONDS = 0.1


class EnrichedRepositoryRefreshError(RuntimeError):
    """Repository 未能恢复到已发布的 enriched generation。"""

    def __init__(
        self,
        refresh_error: Exception,
        operation_error: Exception | None = None,
    ) -> None:
        self.refresh_error = refresh_error
        self.operation_error = operation_error
        message = "enriched 已发布，但 Repository 快照刷新失败；实时行情保持暂停"
        if operation_error is not None:
            message = f"{operation_error}; {message}"
        super().__init__(message)


def _refresh_repository_with_retry(repo) -> None:
    deadline = time.monotonic() + _REPOSITORY_REFRESH_WAIT_SECONDS
    while True:
        try:
            refreshed_generation = repo.refresh_cache(
                enriched_wait_deadline=deadline,
            )
            current_generation = repo.get_matrix_data_generation("stock")
            if (
                not isinstance(refreshed_generation, str)
                or refreshed_generation != current_generation
            ):
                raise RuntimeError(
                    "Repository 未确认装载当前 ready enriched generation"
                )
            return
        except Exception as exc:  # noqa: BLE001
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EnrichedRepositoryRefreshError(exc) from exc
            time.sleep(min(_REPOSITORY_REFRESH_POLL_SECONDS, remaining))


def run_enriched_job_with_repository_refresh(
    repo,
    operation,
    quote_service=None,
):
    """暂停实时写入执行 enriched 任务，并恢复已发布 generation 的快照。"""
    def generation() -> tuple[bool, str | None]:
        try:
            return True, repo.get_matrix_data_generation("stock")
        except Exception:  # noqa: BLE001
            logger.warning("read enriched generation failed", exc_info=True)
            return False, None

    def refresh_required(
        before: tuple[bool, str | None],
        after: tuple[bool, str | None],
    ) -> bool:
        before_known, before_value = before
        after_known, after_value = after
        return not before_known or not after_known or after_value != before_value

    def run():
        before = generation()
        try:
            result = operation()
        except Exception as operation_error:  # noqa: BLE001
            after = generation()
            if not refresh_required(before, after):
                raise
            try:
                _refresh_repository_with_retry(repo)
            except EnrichedRepositoryRefreshError as refresh_error:
                raise EnrichedRepositoryRefreshError(
                    refresh_error.refresh_error,
                    operation_error,
                ) from refresh_error
            raise

        after = generation()
        succeeded = not isinstance(result, dict) or "error" not in result
        if succeeded or refresh_required(before, after):
            _refresh_repository_with_retry(repo)
        return result

    if quote_service is None:
        return run()
    quote_service.pause()
    resume_realtime = True
    try:
        return run()
    except EnrichedRepositoryRefreshError:
        resume_realtime = False
        logger.exception("repository refresh failed; realtime quotes remain paused")
        raise
    finally:
        if resume_realtime:
            quote_service.resume()
