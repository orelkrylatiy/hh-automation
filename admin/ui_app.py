from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse

from .app import app as backend_app
from .ops_console import install_ops_routes
from .ui_shell import render_index_html

_ADMIN_DIR = Path(__file__).parent
_INDEX_PATH = _ADMIN_DIR / "index.html"
_CSS_PATH = _ADMIN_DIR / "ui.css"
_JS_PATH = _ADMIN_DIR / "ui.js"
_OPS_CSS_PATH = _ADMIN_DIR / "ops_ui.css"
_OPS_JS_PATH = _ADMIN_DIR / "ops_ui.js"
_UI_GET_PATHS = {
    "/",
    "/admin-ui.css",
    "/admin-ui.js",
    "/admin-ops.css",
    "/admin-ops.js",
}


def _is_replaced_ui_route(route: object) -> bool:
    path = getattr(route, "path", None)
    methods = getattr(route, "methods", None) or set()
    return path in _UI_GET_PATHS and "GET" in methods


def install_ui_routes(target: FastAPI) -> FastAPI:
    """Replace only the legacy HTML shell while preserving backend/API routes."""
    target.router.routes[:] = [
        route for route in target.router.routes if not _is_replaced_ui_route(route)
    ]

    @target.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> HTMLResponse:
        if not _INDEX_PATH.exists():
            return HTMLResponse("<h1>index.html not found</h1>", status_code=404)
        html = render_index_html(_INDEX_PATH.read_text(encoding="utf-8"))
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-store"},
        )

    @target.get("/admin-ui.css", include_in_schema=False)
    def admin_ui_css() -> FileResponse:
        return FileResponse(
            _CSS_PATH,
            media_type="text/css; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @target.get("/admin-ui.js", include_in_schema=False)
    def admin_ui_js() -> FileResponse:
        return FileResponse(
            _JS_PATH,
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @target.get("/admin-ops.css", include_in_schema=False)
    def admin_ops_css() -> FileResponse:
        return FileResponse(
            _OPS_CSS_PATH,
            media_type="text/css; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @target.get("/admin-ops.js", include_in_schema=False)
    def admin_ops_js() -> FileResponse:
        return FileResponse(
            _OPS_JS_PATH,
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    return target


# ``admin.app`` remains the backend/data source of truth. The ops layer swaps
# only unsafe legacy run endpoints for the same wrappers used by cron; the UI
# layer then replaces the HTML shell and local visual assets.
app = install_ui_routes(install_ops_routes(backend_app))


if __name__ == "__main__":
    import uvicorn

    from hh_applicant_tool import constants

    uvicorn.run(
        "admin.ui_app:app",
        host=constants.ADMIN_LOCALHOST,
        port=constants.ADMIN_DEFAULT_PORT,
        reload=True,
    )
