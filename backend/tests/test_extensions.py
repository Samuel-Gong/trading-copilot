from __future__ import annotations

import types

import pytest
from fastapi import APIRouter, FastAPI

from app.extensions.contracts import (
    BACKEND_EXTENSION_API_VERSION,
    NotificationFormatContext,
    NotificationFormatter,
)
from app.extensions.loader import (
    _validate_router_conflicts,
    configure_backend_extensions,
    start_backend_extensions,
)
from app.extensions.registry import BackendExtensionRegistrar, BackendExtensionRegistry
from app.services.quote_service import QuoteService


class PrefixFormatter(NotificationFormatter):
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def format_message(self, event: dict, context: NotificationFormatContext) -> str:
        assert context.api_version == BACKEND_EXTENSION_API_VERSION
        return f"{self.prefix}{event['message']}"


class BrokenFormatter(NotificationFormatter):
    def format_message(self, event: dict, context: NotificationFormatContext) -> str:
        del event, context
        raise RuntimeError("broken formatter")


def _registrar(extension_id: str = "company.test") -> BackendExtensionRegistrar:
    return BackendExtensionRegistrar(
        extension_id,
        api_version=BACKEND_EXTENSION_API_VERSION,
    )


def test_empty_registry_preserves_existing_notification_objects() -> None:
    registry = BackendExtensionRegistry()
    registry.freeze()
    service = QuoteService()
    service._app_state = types.SimpleNamespace(extension_registry=registry)
    events = [{"message": "原始消息", "source": "strategy"}]

    result = service._format_extension_notifications(events)

    assert result is events
    assert result[0] is events[0]


def test_notification_formatters_are_ordered_and_do_not_mutate_input() -> None:
    registry = BackendExtensionRegistry()
    registrar = _registrar()
    registrar.register_notification_formatter("company.second", PrefixFormatter("B"), order=20)
    registrar.register_notification_formatter("company.first", PrefixFormatter("A"), order=10)
    registry.register(registrar)
    registry.freeze()
    service = QuoteService()
    service._app_state = types.SimpleNamespace(extension_registry=registry)
    events = [{"message": "原始消息", "source": "strategy"}]

    result = service._format_extension_notifications(events)

    assert result == [{"message": "BA原始消息", "source": "strategy"}]
    assert events == [{"message": "原始消息", "source": "strategy"}]
    assert result is not events
    assert result[0] is not events[0]


def test_broken_formatter_keeps_previous_message_and_later_formatters_run() -> None:
    registry = BackendExtensionRegistry()
    registrar = _registrar()
    registrar.register_notification_formatter("company.first", PrefixFormatter("A"), order=10)
    registrar.register_notification_formatter("company.broken", BrokenFormatter(), order=20)
    registrar.register_notification_formatter("company.last", PrefixFormatter("B"), order=30)
    registry.register(registrar)
    registry.freeze()
    service = QuoteService()
    service._app_state = types.SimpleNamespace(extension_registry=registry)

    result = service._format_extension_notifications([{"message": "原始消息"}])

    assert result[0]["message"] == "BA原始消息"


def test_registry_rejects_version_mismatch_without_partial_registration() -> None:
    registry = BackendExtensionRegistry()
    registrar = BackendExtensionRegistrar("company.future", api_version=999)
    registrar.register_notification_formatter("company.future", PrefixFormatter("x"))

    with pytest.raises(ValueError, match="requires backend API"):
        registry.register(registrar)

    registry.freeze()
    assert not registry.has_customizations
    assert not registry.has_notification_formatters


def test_registry_is_frozen_after_startup() -> None:
    registry = BackendExtensionRegistry()
    registry.freeze()

    with pytest.raises(RuntimeError, match="frozen"):
        registry.register(_registrar())


def test_loader_isolates_failed_setup_and_registers_valid_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = types.ModuleType("app.custom.broken")
    broken.EXTENSION_ID = "company.broken"
    broken.EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

    def broken_setup(registrar: BackendExtensionRegistrar) -> None:
        registrar.register_notification_formatter("company.partial", PrefixFormatter("x"))
        raise RuntimeError("setup failed")

    broken.setup = broken_setup

    valid = types.ModuleType("app.custom.valid")
    valid.EXTENSION_ID = "company.valid"
    valid.EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

    def valid_setup(registrar: BackendExtensionRegistrar) -> None:
        router = APIRouter(prefix="/api/custom/valid")

        @router.get("/status")
        def status() -> dict:
            return {"status": "ok"}

        registrar.include_router(router)

    valid.setup = valid_setup
    modules = {broken.__name__: broken, valid.__name__: valid}

    monkeypatch.setattr(
        "app.extensions.loader._custom_module_names",
        lambda: [broken.__name__, valid.__name__],
    )
    monkeypatch.setattr(
        "app.extensions.loader.importlib.import_module",
        lambda name: modules[name],
    )
    app = FastAPI()

    registry, errors = configure_backend_extensions(app)

    assert registry.frozen
    assert registry.extension_ids() == frozenset({"company.valid"})
    assert len(errors) == 1
    assert errors[0].module == broken.__name__
    assert any(getattr(route, "path", None) == "/api/custom/valid/status" for route in app.routes)


def test_startup_runs_only_module_that_registered_duplicate_extension_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[str] = []
    valid = types.ModuleType("app.custom.first")
    valid.EXTENSION_ID = "company.same"
    valid.EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
    valid.setup = lambda registrar: None
    valid.startup = lambda context: started.append(valid.__name__)

    duplicate = types.ModuleType("app.custom.second")
    duplicate.EXTENSION_ID = "company.same"
    duplicate.EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
    duplicate.setup = lambda registrar: None
    duplicate.startup = lambda context: started.append(duplicate.__name__)
    modules = {valid.__name__: valid, duplicate.__name__: duplicate}

    monkeypatch.setattr(
        "app.extensions.loader._custom_module_names",
        lambda: [valid.__name__, duplicate.__name__],
    )
    monkeypatch.setattr(
        "app.extensions.loader.importlib.import_module",
        lambda name: modules[name],
    )

    registry, errors = configure_backend_extensions(FastAPI())
    start_backend_extensions(types.SimpleNamespace(), registry)

    assert registry.extension_ids() == frozenset({"company.same"})
    assert len(errors) == 1
    assert started == [valid.__name__]


def test_extension_static_route_rejected_when_core_dynamic_route_matches() -> None:
    app = FastAPI()

    @app.delete("/api/watchlist/{symbol}")
    def delete_watchlist(symbol: str) -> dict:
        return {"symbol": symbol}

    registrar = _registrar()
    router = APIRouter(prefix="/api/watchlist")

    @router.delete("/special")
    def delete_special() -> dict:
        return {"ok": True}

    registrar.include_router(router)

    with pytest.raises(ValueError, match=r"DELETE /api/watchlist/special"):
        _validate_router_conflicts(app, registrar)


def test_extension_static_route_allowed_when_core_converter_cannot_match() -> None:
    app = FastAPI()

    @app.delete("/api/items/{item_id:int}")
    def delete_item(item_id: int) -> dict:
        return {"item_id": item_id}

    registrar = _registrar()
    router = APIRouter(prefix="/api/items")

    @router.delete("/special")
    def delete_special() -> dict:
        return {"ok": True}

    registrar.include_router(router)

    _validate_router_conflicts(app, registrar)


def test_extension_multi_converter_route_rejected_when_joint_values_overlap() -> None:
    app = FastAPI()

    @app.get("/api/items/{first:int}/{second:float}")
    def get_core_item(first: int, second: float) -> dict:
        return {"first": first, "second": second}

    registrar = _registrar()
    router = APIRouter()

    @router.get("/api/items/{first:float}/{second:int}")
    def get_extension_item(first: float, second: int) -> dict:
        return {"first": first, "second": second}

    registrar.include_router(router)

    with pytest.raises(
        ValueError,
        match=r"GET /api/items/\{first:float\}/\{second:int\}",
    ):
        _validate_router_conflicts(app, registrar)


def test_extension_dynamic_route_allowed_when_converters_are_disjoint() -> None:
    app = FastAPI()

    @app.get("/api/items/{item_id:int}")
    def get_core_item(item_id: int) -> dict:
        return {"item_id": item_id}

    registrar = _registrar()
    router = APIRouter()

    @router.get("/api/items/{item_id:uuid}")
    def get_extension_item(item_id: str) -> dict:
        return {"item_id": item_id}

    registrar.include_router(router)

    _validate_router_conflicts(app, registrar)


def test_extension_path_converter_route_rejected_when_globs_overlap() -> None:
    app = FastAPI()

    @app.get("/a/{value:path}/foo")
    def get_core_path(value: str) -> dict:
        return {"value": value}

    registrar = _registrar()
    router = APIRouter()

    @router.get("/a/bar/{value:path}")
    def get_extension_path(value: str) -> dict:
        return {"value": value}

    registrar.include_router(router)

    with pytest.raises(ValueError, match=r"GET /a/bar/\{value:path\}"):
        _validate_router_conflicts(app, registrar)


def test_extension_path_converter_route_allowed_when_prefixes_are_disjoint() -> None:
    app = FastAPI()

    @app.get("/api/files/{value:path}")
    def get_core_path(value: str) -> dict:
        return {"value": value}

    registrar = _registrar()
    router = APIRouter()

    @router.get("/api/users/{value:path}")
    def get_extension_path(value: str) -> dict:
        return {"value": value}

    registrar.include_router(router)

    _validate_router_conflicts(app, registrar)
