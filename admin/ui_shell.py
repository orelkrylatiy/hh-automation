from __future__ import annotations

import re

_UI_STYLESHEET = '<link rel="stylesheet" href="/admin-ui.css?v=3"/>'
_UI_SCRIPT = '<script src="/admin-ui.js?v=3" defer></script>'
_OPS_SCRIPT = '<script src="/admin-ops.js?v=1" defer></script>'
_ANIMATE_CDN_RE = re.compile(
    r"\s*<link\s+rel=[\"']stylesheet[\"']\s+href=[\"']"
    r"https://cdnjs\.cloudflare\.com/ajax/libs/animate\.css/[^\"']+/animate\.min\.css"
    r"[\"']\s*/?>",
    flags=re.IGNORECASE,
)


def render_index_html(index_html: str) -> str:
    """Inject local visual and operational layers into the legacy frontend."""
    html = _ANIMATE_CDN_RE.sub("", index_html)

    if "/admin-ui.css" not in html:
        if "</head>" not in html:
            raise ValueError("admin index is missing </head>")
        html = html.replace("</head>", f"{_UI_STYLESHEET}\n</head>", 1)

    scripts: list[str] = []
    if "/admin-ui.js" not in html:
        scripts.append(_UI_SCRIPT)
    if "/admin-ops.js" not in html:
        scripts.append(_OPS_SCRIPT)
    if scripts:
        if "</body>" not in html:
            raise ValueError("admin index is missing </body>")
        html = html.replace("</body>", "\n".join(scripts) + "\n</body>", 1)

    return html
