"""
Firebase Cloud Messaging (HTTP v1) sender. Push is a best-effort extra on top of the inbox: nothing in here
may raise into a request handler, and nothing here is required for the app to work.

Configuration (all optional):
  FIREBASE_PROJECT_ID            e.g. "depthflow-app"
  FIREBASE_SERVICE_ACCOUNT_JSON  the service-account JSON as a single-line string, OR
  FIREBASE_SERVICE_ACCOUNT_FILE  path to the service-account .json file
  FCM_DRY_RUN=true               log the message instead of sending it (local dev / tests)
If credentials are missing and dry-run is off, push is simply disabled (inbox still works).

Uses google-auth (already a dependency) to mint the OAuth token; no firebase-admin needed.
"""
import json
import logging
import os
import threading
import time
from typing import Dict, Iterable, List, Optional

import requests

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
ANDROID_CHANNEL_ID = "happy_hour"
_TIMEOUT = 10

_cred_lock = threading.Lock()
_credentials = None
_warned_disabled = False


def is_dry_run() -> bool:
    return os.getenv("FCM_DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")


def _project_id() -> Optional[str]:
    return os.getenv("FIREBASE_PROJECT_ID") or None


def _load_service_account_info() -> Optional[dict]:
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            logger.error("FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON")
            return None
    path = os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE")
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            logger.error(f"Could not read FIREBASE_SERVICE_ACCOUNT_FILE: {e}")
    return None


def is_enabled() -> bool:
    """True if a push can be delivered or (dry-run) logged."""
    if is_dry_run():
        return True
    return bool(_project_id()) and (bool(os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON"))
                                    or bool(os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE")))


def _access_token() -> Optional[str]:
    """OAuth2 access token for FCM, cached by google-auth until shortly before it expires."""
    global _credentials
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GoogleRequest

    with _cred_lock:
        if _credentials is None:
            info = _load_service_account_info()
            if not info:
                return None
            _credentials = service_account.Credentials.from_service_account_info(info, scopes=[FCM_SCOPE])
        if not _credentials.valid:
            _credentials.refresh(GoogleRequest())
        return _credentials.token


def build_message(token: str, title: str, body: str, data: Optional[Dict[str, object]] = None) -> dict:
    """FCM v1 message. `data` values must be strings, so everything is stringified."""
    str_data = {k: ("" if v is None else str(v)) for k, v in (data or {}).items()}
    return {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": str_data,
            "android": {
                "priority": "HIGH",
                "notification": {"channel_id": ANDROID_CHANNEL_ID},
            },
        }
    }


def _is_dead_token(resp: requests.Response) -> bool:
    if resp.status_code == 404:
        return True
    try:
        details = resp.json().get("error", {}).get("details", [])
    except ValueError:
        return False
    return any(d.get("errorCode") == "UNREGISTERED" for d in details if isinstance(d, dict))


def send_to_token(token: str, title: str, body: str, data: Optional[Dict[str, object]] = None) -> str:
    """
    Send one push. Returns one of: "sent", "dry_run", "disabled", "dead_token", "failed". Never raises.
    "dead_token" means FCM said the token is no longer valid (the caller should disable it).
    """
    global _warned_disabled
    try:
        message = build_message(token, title, body, data)
        if is_dry_run():
            logger.info("[FCM dry-run] %s", json.dumps(message, ensure_ascii=False))
            return "dry_run"
        if not is_enabled():
            if not _warned_disabled:
                logger.warning("FCM push disabled: set FIREBASE_PROJECT_ID and a service account "
                               "(FIREBASE_SERVICE_ACCOUNT_JSON/_FILE). Inbox notifications still work.")
                _warned_disabled = True
            return "disabled"

        access_token = _access_token()
        if not access_token:
            return "disabled"
        url = f"https://fcm.googleapis.com/v1/projects/{_project_id()}/messages:send"
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; UTF-8"}

        for attempt in (1, 2):
            resp = requests.post(url, headers=headers, json=message, timeout=_TIMEOUT)
            if resp.status_code == 200:
                return "sent"
            if _is_dead_token(resp):
                return "dead_token"
            if resp.status_code in (429, 500, 502, 503, 504) and attempt == 1:
                time.sleep(1.0)
                continue
            logger.warning("FCM send failed (%s): %s", resp.status_code, resp.text[:300])
            return "failed"
        return "failed"
    except Exception as e:  # push must never break the caller
        logger.warning("FCM send error: %s", e)
        return "failed"


def send_to_tokens(tokens: Iterable[str], title: str, body: str,
                   data: Optional[Dict[str, object]] = None) -> Dict[str, List[str]]:
    """Send to several tokens sequentially. Returns {"dead": [tokens FCM rejected], "sent": [...], ...}."""
    result: Dict[str, List[str]] = {"sent": [], "dead": [], "failed": [], "skipped": []}
    for t in tokens:
        outcome = send_to_token(t, title, body, data)
        if outcome in ("sent", "dry_run"):
            result["sent"].append(t)
        elif outcome == "dead_token":
            result["dead"].append(t)
        elif outcome == "failed":
            result["failed"].append(t)
        else:
            result["skipped"].append(t)
    return result
