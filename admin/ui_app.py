from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse, HTMLResponse

from .app import app
from .ui_shell import render_index_html

_ADMIN_DIR = Path(__file__).parent
_INDEX_PATH = _ADMIN_DIR / "index.html"
_CSS_PATH = _ADMIN_DIR / "ui.css"
_JS_PATH = _ADMIN_DIR / "ui.js"


def _is_legacy_root_route(route: object) -> bool:
    path = getattr(route, "path", None)
    methods = getattr(route, "methods", None) or set()
    return path == "/" and "GET" in methods


# ``admin.app`` remains the backend source of truth. Replace only its legacy
# index route so all API routes, middleware and auth behaviour stay identical.
app.router.routes[:] = [
    route for route in app.router.routes if not _is_legacy_root_route(route)
]


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    if not _INDEX_PATH.exists():
        return HTMLResponse("<h1>index.html not found</h1>", status_code=404)
    html = render_index_html(_INDEX_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/admin-ui.css", include_in_schema=False)
def admin_ui_css() -> FileResponse:
    return FileResponse(
        _CSS_PATH,
        media_type="text/css; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/admin-ui.js", include_in_schema=False)
def admin_ui_js() -> FileResponse:
    return FileResponse(
        _JS_PATH,
        media_type="text/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


if __name__ == "__main__":
    import uvicorn

    from hh_applicant_tool import constants

    uvicorn.run(
        "admin.ui_app:app",
        host=constants.ADMIN_LOCALHOST,
        port=constants.ADMIN_DEFAULT_PORT,
        reload=True,
    )
