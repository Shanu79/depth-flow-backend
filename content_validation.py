"""
Validation / sanitising helpers for admin-managed public content (popups, offer banner, pricing text).

Rules: all text is stored as PLAIN TEXT (HTML tags stripped, control characters removed) and the frontend must
render it as text, never as HTML. Links must be https:// or an in-app path ("/pricing"). Images are accepted
only when the file's magic bytes say JPEG / PNG / WebP (never SVG/HTML), capped in size, and saved under a
random name (the client's filename/extension is ignored).
"""
import os
import re
import uuid
from typing import Optional
from urllib.parse import urlparse

MAX_IMAGE_BYTES = 2 * 1024 * 1024
UPLOAD_ROOT = os.path.join("static", "uploads", "content")   # tests may point this elsewhere

_TAG_RE = re.compile(r"<[^>]*>")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_COUPON_RE = re.compile(r"^[A-Z0-9_-]{3,32}$")


def clean_text(value: Optional[str], *, max_len: int, multiline: bool = False) -> Optional[str]:
    """Strip tags + control chars, trim, collapse whitespace. Returns None for empty input."""
    if value is None:
        return None
    s = _CTRL_RE.sub("", str(value))
    s = _TAG_RE.sub("", s)
    if multiline:
        s = re.sub(r"[ \t]+", " ", s)
        s = re.sub(r"\n{3,}", "\n\n", s.replace("\r\n", "\n").replace("\r", "\n"))
    else:
        s = re.sub(r"\s+", " ", s)
    s = s.strip()
    if not s:
        return None
    if len(s) > max_len:
        raise ValueError(f"must be at most {max_len} characters")
    return s


def clean_link(value: Optional[str]) -> Optional[str]:
    """Button/CTA destination: absolute https URL or an in-app path starting with a single '/'."""
    if value is None or not str(value).strip():
        return None
    s = str(value).strip()
    if len(s) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", s):
        raise ValueError("is not a valid link")
    if s.startswith("/") and not s.startswith("//") and "\\" not in s:
        return s
    u = urlparse(s)
    if u.scheme == "https" and u.netloc and "@" not in u.netloc:
        return s
    raise ValueError("must be an https:// URL or an in-app path starting with '/'")


def clean_image_url(value: Optional[str]) -> Optional[str]:
    """Image source: http(s) URL or an in-app path (the upload endpoint returns BASE_URL + /static/...)."""
    if value is None or not str(value).strip():
        return None
    s = str(value).strip()
    if len(s) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", s):
        raise ValueError("is not a valid image URL")
    if s.startswith("/") and not s.startswith("//"):
        return s
    u = urlparse(s)
    if u.scheme in ("http", "https") and u.netloc:
        return s
    raise ValueError("must be an http(s) URL or an in-app path")


def clean_coupon(value: Optional[str]) -> Optional[str]:
    if value is None or not str(value).strip():
        return None
    s = str(value).strip().upper()
    if not _COUPON_RE.match(s):
        raise ValueError("coupon code must be 3-32 characters: letters, digits, '-' or '_'")
    return s


def sniff_image_ext(head: bytes) -> Optional[str]:
    """Detect the real image type from magic bytes."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def save_image_bytes(data: bytes) -> str:
    """Validate + store; returns the file name. Raises ValueError (message safe to show the admin)."""
    if not data:
        raise ValueError("The uploaded file is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise OverflowError(f"Image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
    ext = sniff_image_ext(data[:16])
    if not ext:
        raise ValueError("Only JPEG, PNG or WebP images are allowed.")
    os.makedirs(UPLOAD_ROOT, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(UPLOAD_ROOT, name), "wb") as f:
        f.write(data)
    return name


def public_url_for(file_name: str) -> str:
    base = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    rel = UPLOAD_ROOT.replace("\\", "/")
    if rel.startswith("static/"):
        rel = rel[len("static/"):]
    return f"{base}/static/{rel}/{file_name}"
