from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from camera import CameraError
from config import CAMERA_SNAPSHOTS_DIR
from dashboard_assets import (
    ALERTS_PAGE_HTML,
    DASHBOARD_PAGE_HTML,
    EVENTS_PAGE_HTML,
    FAULTS_PAGE_HTML,
    FIRING_LOGS_PAGE_HTML,
    PANEL_PAGE_HTML,
    resolve_static_path,
)
from dashboard_services import (
    FIRING_LOG_PHOTOS_DIR,
    activate_profile,
    build_firing_log_markdown,
    capture_camera_snapshot,
    clone_alert_rule,
    create_alert_rule,
    create_event_marker,
    create_firing_log,
    create_profile,
    delete_alert_rule,
    delete_profile,
    fetch_alert_channel_settings,
    fetch_alert_channel_status,
    fetch_alert_deliveries,
    fetch_alert_rules,
    fetch_camera_status,
    fetch_dashboard_preferences,
    fetch_dashboard_status,
    fetch_events,
    fetch_faults,
    fetch_firing_log_detail,
    fetch_firing_logs,
    fetch_history_between,
    fetch_history_with_resolution,
    fetch_profiles,
    fetch_recent_alerts,
    fetch_watchdog_settings,
    refresh_firing_log_related_data,
    reset_alerts,
    reset_faults,
    send_test_alert,
    stop_profile_tracking,
    update_alert_channel_settings,
    update_alert_rule,
    update_dashboard_preferences,
    update_firing_log,
    update_profile,
    update_watchdog_settings,
    upload_firing_log_photo,
)

def handle_get(handler) -> bool:
    parsed_path = urlparse(handler.path)

    static_path = resolve_static_path(parsed_path.path)
    if static_path is not None:
        handler.send_file_response(static_path)
        return True

    if parsed_path.path == "/":
        handler.send_text_response(DASHBOARD_PAGE_HTML, content_type="text/html; charset=utf-8")
        return True

    if parsed_path.path == "/logs":
        handler.send_text_response(FIRING_LOGS_PAGE_HTML, content_type="text/html; charset=utf-8")
        return True

    if parsed_path.path == "/alerts":
        handler.send_text_response(ALERTS_PAGE_HTML, content_type="text/html; charset=utf-8")
        return True

    if parsed_path.path == "/panel":
        handler.send_text_response(PANEL_PAGE_HTML, content_type="text/html; charset=utf-8")
        return True

    if parsed_path.path == "/events":
        handler.send_text_response(EVENTS_PAGE_HTML, content_type="text/html; charset=utf-8")
        return True

    if parsed_path.path == "/faults":
        handler.send_text_response(FAULTS_PAGE_HTML, content_type="text/html; charset=utf-8")
        return True

    if parsed_path.path == "/camera/latest.jpg":
        latest_path = CAMERA_SNAPSHOTS_DIR / "latest.jpg"
        if not latest_path.exists():
            handler.send_error(404, "No snapshot available")
            return True
        try:
            image_bytes = latest_path.read_bytes()
        except OSError:
            handler.send_error(500, "Unable to read snapshot")
            return True
        handler.send_response(200)
        handler.send_header("Content-Type", "image/jpeg")
        handler.send_header("Content-Length", str(len(image_bytes)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(image_bytes)
        return True

    if parsed_path.path.startswith("/camera/archive/"):
        filename = unquote(parsed_path.path.removeprefix("/camera/archive/"))
        if not filename or Path(filename).name != filename:
            handler.send_error(400, "Invalid snapshot filename")
            return True
        archived_path = CAMERA_SNAPSHOTS_DIR / filename
        if not archived_path.exists():
            handler.send_error(404, "Snapshot not found")
            return True
        try:
            image_bytes = archived_path.read_bytes()
        except OSError:
            handler.send_error(500, "Unable to read snapshot")
            return True
        handler.send_response(200)
        handler.send_header("Content-Type", "image/jpeg")
        handler.send_header("Content-Length", str(len(image_bytes)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(image_bytes)
        return True

    if parsed_path.path.startswith("/firing-log-photos/"):
        filename = unquote(parsed_path.path.removeprefix("/firing-log-photos/"))
        if not filename or Path(filename).name != filename:
            handler.send_error(400, "Invalid firing log photo filename")
            return True
        photo_path = FIRING_LOG_PHOTOS_DIR / filename
        if not photo_path.exists():
            handler.send_error(404, "Firing log photo not found")
            return True
        try:
            image_bytes = photo_path.read_bytes()
        except OSError:
            handler.send_error(500, "Unable to read firing log photo")
            return True
        mime_type = "image/jpeg"
        if photo_path.suffix.lower() == ".png":
            mime_type = "image/png"
        elif photo_path.suffix.lower() == ".webp":
            mime_type = "image/webp"
        handler.send_response(200)
        handler.send_header("Content-Type", mime_type)
        handler.send_header("Content-Length", str(len(image_bytes)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(image_bytes)
        return True

    if parsed_path.path == "/api/status":
        handler.send_json_response(fetch_dashboard_status())
        return True

    if parsed_path.path == "/api/history":
        query = parse_qs(parsed_path.query)
        range_name = query.get("range", ["24h"])[0]
        resolution_name = query.get("resolution", ["auto"])[0]
        start_text = query.get("start", [""])[0].strip()
        end_text = query.get("end", [""])[0].strip()
        if start_text and end_text:
            try:
                start_dt = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
            except ValueError:
                handler.send_json_response({"error": "Invalid start or end timestamp."}, status=400)
                return True
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            handler.send_json_response(fetch_history_between(start_dt, end_dt, resolution_name))
        else:
            handler.send_json_response(fetch_history_with_resolution(range_name, resolution_name))
        return True

    if parsed_path.path == "/api/alert-rules":
        handler.send_json_response(fetch_alert_rules())
        return True

    if parsed_path.path == "/api/alert-deliveries":
        handler.send_json_response(fetch_alert_deliveries())
        return True

    if parsed_path.path == "/api/recent-alerts":
        handler.send_json_response(fetch_recent_alerts())
        return True

    if parsed_path.path == "/api/firing-logs":
        query = parse_qs(parsed_path.query)
        limit = int(query.get("limit", ["100"])[0])
        handler.send_json_response(fetch_firing_logs(limit=max(1, min(limit, 500))))
        return True

    if parsed_path.path.startswith("/api/firing-logs/") and parsed_path.path.endswith("/export.md"):
        firing_log_id = int(parsed_path.path.split("/")[3])
        markdown_body = build_firing_log_markdown(firing_log_id)
        handler.send_text_response(markdown_body, content_type="text/markdown; charset=utf-8")
        return True

    if parsed_path.path.startswith("/api/firing-logs/"):
        firing_log_id = int(parsed_path.path.split("/")[3])
        handler.send_json_response(fetch_firing_log_detail(firing_log_id))
        return True

    if parsed_path.path == "/api/events":
        query = parse_qs(parsed_path.query)
        limit = int(query.get("limit", ["100"])[0])
        event_type = query.get("event_type", [""])[0].strip() or None
        search = query.get("search", [""])[0].strip() or None
        start = query.get("start", [""])[0].strip() or None
        end = query.get("end", [""])[0].strip() or None
        handler.send_json_response(
            fetch_events(
                limit=max(1, min(limit, 500)),
                event_type=event_type,
                search=search,
                start=start,
                end=end,
            )
        )
        return True

    if parsed_path.path == "/api/faults":
        query = parse_qs(parsed_path.query)
        range_name = query.get("range", ["24h"])[0]
        limit = int(query.get("limit", ["100"])[0])
        search = query.get("search", [""])[0].strip() or None
        start = query.get("start", [""])[0].strip() or None
        end = query.get("end", [""])[0].strip() or None
        min_temp_text = query.get("min_temp_f", [""])[0].strip()
        max_temp_text = query.get("max_temp_f", [""])[0].strip()
        min_temp_f = float(min_temp_text) if min_temp_text else None
        max_temp_f = float(max_temp_text) if max_temp_text else None
        handler.send_json_response(
            fetch_faults(
                range_name,
                limit=max(1, min(limit, 500)),
                search=search,
                min_temp_f=min_temp_f,
                max_temp_f=max_temp_f,
                start=start,
                end=end,
            )
        )
        return True

    if parsed_path.path == "/api/alert-channels":
        handler.send_json_response(fetch_alert_channel_status())
        return True

    if parsed_path.path == "/api/alert-channel-settings":
        handler.send_json_response(fetch_alert_channel_settings())
        return True

    if parsed_path.path == "/api/watchdog-settings":
        handler.send_json_response(fetch_watchdog_settings())
        return True

    if parsed_path.path == "/api/profiles":
        handler.send_json_response(fetch_profiles())
        return True

    if parsed_path.path == "/api/camera/status":
        handler.send_json_response(fetch_camera_status())
        return True

    if parsed_path.path == "/api/dashboard-preferences":
        handler.send_json_response(fetch_dashboard_preferences())
        return True

    handler.send_error(404, "Not Found")

def handle_post(handler) -> bool:
    parsed_path = urlparse(handler.path)

    try:
        content_length = int(handler.headers.get("Content-Length", "0"))
        raw_body = handler.rfile.read(content_length) if content_length else b"{}"
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        handler.send_json_response({"error": "Invalid JSON body."}, status=400)
        return True

    try:
        if parsed_path.path == "/api/alert-rules":
            handler.send_json_response(create_alert_rule(payload))
            return True

        if parsed_path.path == "/api/dashboard-preferences":
            handler.send_json_response(update_dashboard_preferences(payload))
            return True

        if parsed_path.path == "/api/alert-channel-settings":
            handler.send_json_response(update_alert_channel_settings(payload))
            return True

        if parsed_path.path == "/api/watchdog-settings":
            handler.send_json_response(update_watchdog_settings(payload))
            return True

        if parsed_path.path == "/api/profiles":
            handler.send_json_response(create_profile(payload))
            return True

        if parsed_path.path == "/api/firing-logs":
            handler.send_json_response(create_firing_log(payload))
            return True

        if parsed_path.path == "/api/events":
            handler.send_json_response(create_event_marker(payload))
            return True

        if parsed_path.path == "/api/camera/capture":
            handler.send_json_response(capture_camera_snapshot())
            return True

        if parsed_path.path == "/api/profiles/stop":
            handler.send_json_response(stop_profile_tracking())
            return True

        if parsed_path.path == "/api/reset-faults":
            handler.send_json_response(reset_faults())
            return True

        if parsed_path.path == "/api/reset-alerts":
            handler.send_json_response(reset_alerts())
            return True

        if parsed_path.path == "/api/test-alert":
            handler.send_json_response(send_test_alert(payload))
            return True

        if parsed_path.path.startswith("/api/alert-rules/") and parsed_path.path.endswith("/delete"):
            rule_id = int(parsed_path.path.split("/")[3])
            handler.send_json_response(delete_alert_rule(rule_id))
            return True

        if parsed_path.path.startswith("/api/alert-rules/") and parsed_path.path.endswith("/clone"):
            rule_id = int(parsed_path.path.split("/")[3])
            handler.send_json_response(clone_alert_rule(rule_id))
            return True

        if parsed_path.path.startswith("/api/profiles/") and parsed_path.path.endswith("/delete"):
            profile_id = int(parsed_path.path.split("/")[3])
            handler.send_json_response(delete_profile(profile_id))
            return True

        if parsed_path.path.startswith("/api/profiles/") and parsed_path.path.endswith("/activate"):
            profile_id = int(parsed_path.path.split("/")[3])
            handler.send_json_response(activate_profile(profile_id))
            return True

        if parsed_path.path.startswith("/api/firing-logs/") and parsed_path.path.endswith("/refresh"):
            firing_log_id = int(parsed_path.path.split("/")[3])
            handler.send_json_response(refresh_firing_log_related_data(firing_log_id))
            return True

        if parsed_path.path.startswith("/api/firing-logs/") and parsed_path.path.endswith("/photos"):
            firing_log_id = int(parsed_path.path.split("/")[3])
            handler.send_json_response(upload_firing_log_photo(firing_log_id, payload))
            return True

        if parsed_path.path.startswith("/api/alert-rules/"):
            rule_id = int(parsed_path.path.split("/")[3])
            handler.send_json_response(update_alert_rule(rule_id, payload))
            return True

        if parsed_path.path.startswith("/api/firing-logs/"):
            firing_log_id = int(parsed_path.path.split("/")[3])
            handler.send_json_response(update_firing_log(firing_log_id, payload))
            return True

        if parsed_path.path.startswith("/api/profiles/"):
            profile_id = int(parsed_path.path.split("/")[3])
            handler.send_json_response(update_profile(profile_id, payload))
            return True
    except ValueError as exc:
        handler.send_json_response({"error": str(exc)}, status=400)
        return True
    except CameraError as exc:
        handler.send_json_response({"error": str(exc)}, status=500)
        return True
    except sqlite3.Error as exc:
        handler.send_json_response({"error": f"Database error: {exc}"}, status=500)
        return True

    handler.send_json_response({"error": "Not Found"}, status=404)
    return True

