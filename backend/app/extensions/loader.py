"""Discover in-repository backend customizations without touching user data."""
from __future__ import annotations

import importlib
import logging
import pkgutil
import re
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.extensions.contracts import BACKEND_EXTENSION_API_VERSION, ExtensionContext
from app.extensions.registry import BackendExtensionRegistrar, BackendExtensionRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackendExtensionLoadError:
    module: str
    error: str


def _custom_module_names() -> list[str]:
    try:
        package = importlib.import_module("app.custom")
    except ModuleNotFoundError:
        return []
    return sorted(
        item.name
        for item in pkgutil.iter_modules(package.__path__, f"{package.__name__}.")
        if not item.name.rsplit(".", 1)[-1].startswith("_")
    )


def configure_backend_extensions(
    app: FastAPI,
) -> tuple[BackendExtensionRegistry, tuple[BackendExtensionLoadError, ...]]:
    """Import custom modules and register validated routes and policies."""
    registry = BackendExtensionRegistry()
    errors: list[BackendExtensionLoadError] = []

    for module_name in _custom_module_names():
        try:
            module = importlib.import_module(module_name)
            extension_id = getattr(module, "EXTENSION_ID", None)
            api_version = getattr(module, "EXTENSION_API_VERSION", None)
            if not isinstance(extension_id, str):
                raise ValueError("backend extension module must define EXTENSION_ID")
            registrar = BackendExtensionRegistrar(extension_id, api_version=api_version)
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise ValueError("backend extension module must define setup(registrar)")
            setup(registrar)
            _validate_router_conflicts(app, registrar)
            registry.register(registrar, module_name=module_name)
            for router in registrar.routers:
                app.include_router(router)
        except Exception as exc:
            logger.warning("backend extension load failed %s: %s", module_name, exc)
            errors.append(BackendExtensionLoadError(module_name, str(exc)))

    registry.freeze()
    return registry, tuple(errors)


def _validate_router_conflicts(app: FastAPI, registrar: BackendExtensionRegistrar) -> None:
    existing: dict[str, list[APIRoute]] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            existing.setdefault(method, []).append(route)
    staged: dict[str, list[APIRoute]] = {}
    for router in registrar.routers:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods:
                candidates = existing.get(method, []) + staged.get(method, [])
                if any(_route_paths_overlap(route, candidate) for candidate in candidates):
                    raise ValueError(
                        f"extension {registrar.extension_id!r} route conflicts: "
                        f"{method} {route.path}"
                    )
                staged.setdefault(method, []).append(route)


def _route_paths_overlap(left: APIRoute, right: APIRoute) -> bool:
    """判断两个 FastAPI 路径模板是否可能匹配同一请求路径。"""
    if left.path == right.path:
        return True
    left_segments = _simple_route_segments(left)
    right_segments = _simple_route_segments(right)
    if left_segments is not None and right_segments is not None:
        return _route_segment_patterns_overlap(left_segments, right_segments)
    left_sample = _route_sample_path(left)
    right_sample = _route_sample_path(right)
    samples_overlap = bool(
        left.path_regex.fullmatch(right_sample)
        or right.path_regex.fullmatch(left_sample)
    )
    if samples_overlap:
        return True
    return _complex_templates_may_overlap(left.path, right.path)


_FULL_PATH_PARAMETER = re.compile(r"^\{([^}:]+)(?::([^}]+))?\}$")
_BUILTIN_CONVERTER_NAMES = {
    "StringConvertor",
    "IntegerConvertor",
    "FloatConvertor",
    "UUIDConvertor",
}
_CONVERTER_WITNESSES = (
    "1",
    "1.0",
    "sample",
    "00000000-0000-0000-0000-000000000001",
)


def _simple_route_segments(route: APIRoute) -> list[str | object] | None:
    """解析静态段或单转换器段；段内混合模板交给保守回退。"""
    segments: list[str | object] = []
    for segment in route.path.split("/"):
        match = _FULL_PATH_PARAMETER.fullmatch(segment)
        if match is None:
            if "{" in segment or "}" in segment:
                return None
            segments.append(segment)
            continue
        convertor = route.param_convertors.get(match.group(1))
        if convertor is None:
            return None
        segments.append(convertor)
    return segments


def _route_segment_patterns_overlap(
    left: list[str | object],
    right: list[str | object],
) -> bool:
    """用有限状态乘积判断两个分段路径模板是否存在共同请求路径。"""
    pending = [(0, 0)]
    visited: set[tuple[int, int]] = set()
    while pending:
        state = pending.pop()
        if state in visited:
            continue
        visited.add(state)
        left_index, right_index = state
        if left_index == len(left) and right_index == len(right):
            return True
        for left_next, left_segment in _segment_transitions(left, left_index):
            for right_next, right_segment in _segment_transitions(right, right_index):
                if _simple_segments_overlap(left_segment, right_segment):
                    pending.append((left_next, right_next))
    return False


def _segment_transitions(
    segments: list[str | object],
    index: int,
) -> tuple[tuple[int, str | object], ...]:
    if index >= len(segments):
        return ()
    segment = segments[index]
    if type(segment).__name__ == "PathConvertor":
        # `path` 可吞掉当前及后续 URL 段；空值对应尾斜杠产生的空 URL 段。
        return ((index, segment), (index + 1, segment))
    return ((index + 1, segment),)


def _simple_segments_overlap(left: str | object, right: str | object) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, str):
        return bool(re.fullmatch(getattr(right, "regex"), left))
    if isinstance(right, str):
        return bool(re.fullmatch(getattr(left, "regex"), right))

    left_regex = getattr(left, "regex")
    right_regex = getattr(right, "regex")
    if any(
        re.fullmatch(left_regex, witness) and re.fullmatch(right_regex, witness)
        for witness in _CONVERTER_WITNESSES
    ):
        return True
    if {
        type(left).__name__,
        type(right).__name__,
    } <= _BUILTIN_CONVERTER_NAMES:
        return False
    # 自定义转换器没有可证明的交集算法时 fail-closed，避免注册不可达路由。
    return True


def _complex_templates_may_overlap(left: str, right: str) -> bool:
    """段内混合模板只有在字面前后缀可证明冲突时才放行。"""
    left_prefix = left.split("{", 1)[0]
    right_prefix = right.split("{", 1)[0]
    if not (left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)):
        return False
    left_suffix = left.rsplit("}", 1)[-1]
    right_suffix = right.rsplit("}", 1)[-1]
    return left_suffix.endswith(right_suffix) or right_suffix.endswith(left_suffix)


def _route_sample_path(route: APIRoute) -> str:
    """为 Starlette 内置路径转换器生成一个可匹配的代表路径。"""
    samples = {
        "str": "sample",
        "path": "sample/path",
        "int": "1",
        "float": "1.0",
        "uuid": "00000000-0000-0000-0000-000000000001",
    }

    def replace(match: re.Match[str]) -> str:
        converter = match.group(2) or "str"
        return samples.get(converter, "sample")

    return re.sub(r"\{([^}:]+)(?::([^}]+))?\}", replace, route.path)


def start_backend_extensions(
    context: ExtensionContext,
    registry: BackendExtensionRegistry,
) -> None:
    """Run optional post-core startup hooks after the stable context is available."""
    for module_name in _custom_module_names():
        try:
            module = importlib.import_module(module_name)
            if module_name not in registry.module_names():
                continue
            startup = getattr(module, "startup", None)
            if callable(startup):
                startup(context)
        except Exception as exc:
            logger.warning("backend extension startup failed %s: %s", module_name, exc)


def current_extension_context(*, data_dir, repository) -> ExtensionContext:
    return ExtensionContext(
        api_version=BACKEND_EXTENSION_API_VERSION,
        data_dir=data_dir,
        repository=repository,
    )
