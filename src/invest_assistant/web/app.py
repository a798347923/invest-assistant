"""FastAPI 面板：应用工厂（认证中间件 + 登录/includes + 路由挂载）。"""

from __future__ import annotations

import hmac
import logging

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .. import __version__
from ..config import get_settings
from .api import router as api_router
from .pages import router as pages_router
from .templating import TEMPLATES, WEB_DIR

log = logging.getLogger(__name__)


def create_app(settings=None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="invest-assistant", docs_url=None, redoc_url=None)

    password = settings.panel_password or ""

    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        if not password:
            return await call_next(request)
        path = request.url.path
        if path.startswith(("/static", "/login", "/health")):
            return await call_next(request)
        if request.session.get("auth"):
            return await call_next(request)
        if path.startswith("/api"):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=307)

    # SessionMiddleware 必须在 auth_guard 之外层（最后添加的中间件最先执行）。
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        same_site="lax",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(request: Request, password_input: str = Form(..., alias="password")):
        if password and hmac.compare_digest(password_input, password):
            request.session["auth"] = True
            return RedirectResponse("/", status_code=303)
        return TEMPLATES.TemplateResponse(
            request, "login.html", {"error": "密码错误"}, status_code=401
        )

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login" if password else "/", status_code=303)

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    app.include_router(api_router)
    app.include_router(pages_router)
    return app
