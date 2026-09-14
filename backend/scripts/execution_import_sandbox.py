"""只在回环地址启动成交导入联调环境,每次启动使用新的合成数据目录。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import polars as pl
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth as auth_api
from app.api import portfolio as portfolio_api
from app.config import settings
from app.services import auth, portfolio
from app.tickflow.repository import DataStore, KlineRepository


def main() -> None:
    password = os.environ.get("EXECUTION_SANDBOX_PASSWORD")
    if not password:
        raise SystemExit("请通过 EXECUTION_SANDBOX_PASSWORD 提供仅用于合成联调的临时密码")
    with tempfile.TemporaryDirectory(prefix="execution-import-sandbox-") as directory:
        settings.data_dir = Path(directory)
        settings.auth_cookie_secure = False
        store = DataStore(settings.data_dir)
        pl.DataFrame({"symbol": ["600000.SH"], "name": ["合成测试标的"]}).write_parquet(
            settings.data_dir / "instruments" / "stock.parquet",
        )
        auth.set_password(password)
        portfolio.create_account("合成联调账户")
        app = FastAPI()
        app.state.repo = KlineRepository(store)
        app.include_router(auth_api.router)
        app.include_router(portfolio_api.router)

        @app.middleware("http")
        async def require_session(request, call_next):
            if request.url.path.startswith("/api/portfolio"):
                token = request.cookies.get(auth_api.COOKIE_NAME)
                if not token or not auth.is_valid_session(token):
                    from fastapi.responses import JSONResponse
                    return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})
            return await call_next(request)

        @app.get("/api/settings")
        def sandbox_settings():
            return {"onboarding_completed": True}

        @app.get("/api/data/trading-dates")
        def sandbox_dates():
            return {"dates": ["2026-07-30"], "earliest_date": "2026-07-30", "latest_date": "2026-07-30"}

        @app.get("/api/{endpoint:path}")
        def unsupported_endpoint(endpoint: str):
            raise HTTPException(status_code=404, detail="合成沙盒仅提供成交导入和持仓接口")

        frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
        if frontend.exists():
            app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

            @app.get("/{page:path}")
            def page(page: str):
                return FileResponse(frontend / "index.html")

        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("EXECUTION_SANDBOX_PORT", "3038")))


if __name__ == "__main__":
    main()
