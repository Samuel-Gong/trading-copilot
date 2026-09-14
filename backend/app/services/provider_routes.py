"""数据源路由取数结果的提交事务。"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext

from app.services import preferences


@contextmanager
def provider_route_commit_guard(
    provider_name: str,
    generation: int | None,
    preference_getter: Callable[[], str],
    dataset: str,
) -> Iterator[None]:
    """固定偏好与注册表版本，覆盖“校验→发布”的完整临界区。"""
    with preferences.provider_route_lock():
        registry_guard = nullcontext()
        if generation is not None:
            from app.data_providers import custom as custom_sources

            registry_guard = custom_sources.registry_read_lock()
        with registry_guard:
            if preference_getter() != provider_name:
                raise RuntimeError(f"{dataset} provider changed during sync")
            if generation is not None:
                from app.data_providers import custom as custom_sources

                if custom_sources.registry_generation() != generation:
                    raise RuntimeError(
                        f"{dataset} provider configuration changed during sync"
                    )
            yield
