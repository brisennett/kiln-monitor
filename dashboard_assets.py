from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

UI_DIR = Path(__file__).resolve().parent / "ui"
STATIC_DIR = Path(__file__).resolve().parent / "static"

def load_ui_page(filename: str) -> str:
    return (UI_DIR / filename).read_text(encoding="utf-8")

def resolve_static_path(request_path: str) -> Path | None:
    if not request_path.startswith("/static/"):
        return None

    relative_path = Path(unquote(request_path.removeprefix("/static/")))
    candidate = (STATIC_DIR / relative_path).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate

DASHBOARD_PAGE_HTML = load_ui_page("dashboard.html")
ALERTS_PAGE_HTML = load_ui_page("alerts.html")
EVENTS_PAGE_HTML = load_ui_page("events.html")
FAULTS_PAGE_HTML = load_ui_page("faults.html")
FIRING_LOGS_PAGE_HTML = load_ui_page("logs.html")
PANEL_PAGE_HTML = load_ui_page("panel.html")
