"""HimTik: small standard-library HTTP service that delegates media extraction to yt-dlp.

For TikTok/Douyin/Facebook/Instagram public posts only. Doesn't claim guaranteed no-watermark access.

Photo posts from Instagram and Facebook go through gallery-dl instead:
yt-dlp cannot read them (IG photo posts report "No video formats found",
FB requires a login). gallery-dl runs without cookies by default; optional
IG_COOKIES / FB_COOKIES env vars (Netscape format) are written to a temp
file at runtime and passed with --cookies, never logged or stored in the
repo. Every image is served as an individual file (no zip), so the iPhone
Shortcut can save each straight into Photos. For Facebook, when gallery-dl
cannot parse the photo page, the server falls back to fetching the page
with the FB cookies and taking its og:image.

V2: video downloads prefer direct streaming. POST /api/download first resolves the
post's direct MP4 URL (cached metadata) and returns a ready stream job; the client
then fetches GET /api/stream/<job> and the server pipes the video straight through
without saving the file on Render. The stream runs yt-dlp fed by the cached
extraction (--load-info-json), so a link is extracted only once per 10 minutes:
plain fetches of TikTok's CDN URLs are refused from server IPs, so trying them
first only wasted time. When no direct MP4 is available, or streaming fails, the
client falls back to the original full-download job (GET /api/files/<job>).
Audio (MP3) always uses the full-download path.
"""
from __future__ import annotations

import html
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
STORAGE = Path(os.environ.get("HIMTIK_TEMP_DIR", tempfile.gettempdir())) / "himtik-jobs"
MAX_BYTES = 120 * 1024 * 1024
MAX_REQUEST = 4096
JOB_TTL_SECONDS = 20 * 60
MAX_JOBS = 8
MAX_IMAGES = 50
# gallery-dl image path (Instagram/Facebook photo posts, which yt-dlp cannot
# read): probe timeout 60 s; download timeout 180 s like the yt-dlp download
# path, because up to 30 images need more than a minute on the free tier.
GALLERY_PROBE_TIMEOUT = 60
GALLERY_DOWNLOAD_TIMEOUT = 180
MAX_GALLERY_IMAGES = 30
# Phrases gallery-dl prints when the post needs a logged-in session.
_GALLERY_LOGIN_MARKERS = (
    "must be logged in", "redirect to login", "authrequired",
    "login required", "not logged in", "authentication required",
    "log in to continue",
)
# Facebook og:image parser fallback (2.0.6): gallery-dl's Facebook
# extractor cannot parse some photo pages (it crashes, e.g. KeyError
# 'fbid'). Fetch the page with the FB cookies and take the og:image.
FB_PARSER_PAGE_TIMEOUT = 30
FB_PARSER_IMAGE_TIMEOUT = 60
FB_PARSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# Facebook serves a stripped page to bare requests, so the parser sends
# browser-like headers (2.0.7). The Sec-Fetch headers are what gallery-dl's
# Facebook extractor sends too (2.0.11): without them Facebook answers
# HTTP 400 to some URLs (verified locally: /posts/ went 400 -> 200 once
# Sec-Fetch-Dest/Mode/Site were added).
FB_PARSER_HEADERS = {
    "User-Agent": FB_PARSER_UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/png,image/svg+xml,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}
# Gallery errors that must NOT fall back to the Facebook parser: a login
# failure (the parser needs a session too), the 30-image cap, the 120 MB
# cap, and video posts (the parser would return a video thumbnail).
_FB_PARSER_SKIP_PREFIXES = ("Bài này cần đăng nhập", "Album quá nhiều ảnh",
                            "Tổng dung lượng", "Bài đăng này là video")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp")
IMAGE_CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                       ".webp": "image/webp", ".gif": "image/gif",
                       ".avif": "image/avif", ".bmp": "image/bmp"}
# Direct-URL metadata cache: one yt-dlp extraction per link per 10 minutes.
META_CACHE_TTL_SECONDS = 10 * 60
META_CACHE_MAX = 50
# Wall-clock budget for one proxied stream (extraction already happened before).
STREAM_DEADLINE_SECONDS = 240
STREAM_READ_TIMEOUT = 15
STREAM_CHUNK = 256 * 1024
META_CACHE = {}
# Origins allowed to call the API cross-origin (order app tab on GitHub Pages).
ALLOWED_ORIGINS = ("https://kanz510.github.io",)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
JOBS = {}
RATE = {}
LOCK = threading.RLock()
POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media")


def extract_url(raw: str) -> str:
    """Pick a share URL out of pasted text and only accept TikTok/Douyin domains."""
    if not isinstance(raw, str) or len(raw) > 2500:
        raise ValueError("Liên kết không hợp lệ.")
    found = URL_RE.search(raw.strip())
    if not found:
        raise ValueError("Hãy dán liên kết bài đăng TikTok, Douyin, Facebook hoặc Instagram.")
    url = found.group(0).rstrip(".,;!?)]}）。")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    allowed = ("tiktok.com", "douyin.com", "iesdouyin.com", "facebook.com", "instagram.com", "fb.watch")
    if (
        parts.scheme not in ("http", "https")
        or parts.username or parts.password
        or (parts.port not in (None, 80, 443))
        or not any(host == h or host.endswith("." + h) for h in allowed)
        or not parts.path
    ):
        raise ValueError("Chỉ hỗ trợ liên kết công khai từ TikTok, Douyin, Facebook hoặc Instagram.")
    return url


def ytdlp_base(playlist: bool = False) -> list[str]:
    """Base yt-dlp flags. playlist=True drops --no-playlist (image albums only);
    the default keeps it so video/audio behaviour is unchanged."""
    base = [
        os.environ.get("YTDLP_BIN", "yt-dlp"),
        "--no-warnings", "--no-progress",
        "--socket-timeout", "12", "--retries", "1", "--fragment-retries", "1",
    ]
    if not playlist:
        base.insert(1, "--no-playlist")
    return base


def _source_label(extractor_key) -> str:
    """Display platform name from yt-dlp's extractor key."""
    key = (extractor_key or "").lower()
    if "douyin" in key:
        return "Douyin"
    if "facebook" in key:
        return "Facebook"
    if "instagram" in key:
        return "Instagram"
    return "TikTok"


def inspect_media(url: str, force: bool = False, allow_playlist: bool = False) -> dict:
    """Run yt-dlp --dump-single-json once per link (cached 10 minutes).

    Returns the full parsed info (display fields + formats + http_headers) so
    callers can pick a direct MP4 URL without a second extraction run.
    With allow_playlist=True (image mode) a playlist/multi_video is accepted
    and the image count is returned in "images"; the result is NOT cached,
    so a playlist can never leak into the video branch.
    """
    now = time.time()
    if not force and not allow_playlist:
        with LOCK:
            hit = META_CACHE.get(url)
            if hit and now - hit["created"] < META_CACHE_TTL_SECONDS:
                print("[timing] inspect cache-hit", flush=True)
                return hit["info"]
    started = time.monotonic()
    result = subprocess.run(
        ytdlp_base(playlist=allow_playlist) + ["--dump-single-json", "--skip-download", "--no-check-formats", url],
        capture_output=True, text=True, timeout=40, check=False,
    )
    print(f"[timing] inspect {'forced' if force else 'miss'} {time.monotonic() - started:.1f}s rc={result.returncode}", flush=True)
    if result.returncode != 0:
        raise ValueError("Không đọc được bài đăng. Hãy thử video công khai khác hoặc kiểm tra liên kết.")
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ValueError("Nguồn video chưa được hỗ trợ. Vui lòng thử lại sau.") from None
    if not isinstance(info, dict):
        raise ValueError("Bài đăng dạng album/playlist chưa hỗ trợ trong phiên bản này.")
    is_album = info.get("_type") in ("playlist", "multi_video")
    if is_album and not allow_playlist:
        raise ValueError("Bài đăng dạng album/playlist chưa hỗ trợ trong phiên bản này.")
    if not is_album and not info.get("formats") and not info.get("url"):
        raise ValueError("Không tìm thấy luồng video có thể tải cho bài đăng này.")
    if allow_playlist:
        if is_album:
            image_count = len([e for e in (info.get("entries") or []) if isinstance(e, dict)])
            if image_count == 0:
                raise ValueError("Không tìm thấy ảnh nào trong bài đăng này.")
            if image_count > MAX_IMAGES:
                raise ValueError(f"Album quá nhiều ảnh (tối đa {MAX_IMAGES} ảnh).")
        else:
            image_count = 1
    parsed = {
        "title": str(info.get("title") or info.get("description") or "Video TikTok")[:150],
        "creator": str(info.get("uploader") or info.get("creator") or "TikTok")[:80],
        "thumbnail": str(info.get("thumbnail") or "")[:1500],
        "duration": info.get("duration") if isinstance(info.get("duration"), (int, float)) else None,
        "source": _source_label(info.get("extractor_key")),
        "formats": info.get("formats") or [],
        "http_headers": info.get("http_headers") or {},
    }
    if allow_playlist:
        parsed["images"] = image_count
        parsed["_type"] = info.get("_type")
    if not allow_playlist:
        with LOCK:
            META_CACHE[url] = {"created": now, "info": parsed, "raw": info}
            while len(META_CACHE) > META_CACHE_MAX:
                META_CACHE.pop(next(iter(META_CACHE)), None)
    return parsed


def raw_info_for(url: str) -> dict | None:
    """Full yt-dlp info dict for a link, from the metadata cache if fresh.

    Falls back to a (single) fresh extraction when the cache has nothing
    usable. The stream pipe feeds this to yt-dlp via --load-info-json so
    it can start downloading without extracting the post a second time.
    """
    now = time.time()
    with LOCK:
        hit = META_CACHE.get(url)
        if hit and now - hit["created"] < META_CACHE_TTL_SECONDS:
            raw = hit.get("raw")
            if isinstance(raw, dict) and raw.get("formats"):
                return raw
    inspect_media(url, force=True)
    with LOCK:
        hit = META_CACHE.get(url)
        raw = hit.get("raw") if hit else None
        return raw if isinstance(raw, dict) and raw.get("formats") else None


def public_info(info: dict) -> dict:
    """Display fields for /api/inspect (no direct media URLs)."""
    out = {k: info.get(k) for k in ("title", "creator", "thumbnail", "duration", "source")}
    if "images" in info:
        out["images"] = info["images"]
    return out


def pick_direct_list(info: dict) -> list[dict]:
    """Ordered progressive MP4 (video+audio) direct URLs from yt-dlp metadata.

    Prefers TikTok's own 'download' format, then the highest h264+aac MP4,
    then any MP4 with both tracks. Several candidates are returned because
    TikTok serves the same video from multiple CDN hosts and one host can
    refuse a given client while another serves it.
    """
    candidates = []
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        media_url = fmt.get("url") or ""
        if (
            fmt.get("ext") != "mp4"
            or not media_url.startswith("https://")
            or fmt.get("protocol") not in (None, "https")
            or fmt.get("vcodec") in (None, "none")
            or fmt.get("acodec") in (None, "none")
        ):
            continue
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        candidates.append({
            "url": media_url,
            "format_id": str(fmt.get("format_id") or ""),
            "vcodec": str(fmt.get("vcodec") or ""),
            "filesize": size if isinstance(size, int) and size > 0 else None,
            "height": fmt.get("height") if isinstance(fmt.get("height"), int) else 0,
            "http_headers": fmt.get("http_headers") or info.get("http_headers") or {},
        })
    if not candidates:
        return []
    download = [c for c in candidates if c["format_id"] == "download"]
    h264 = [c for c in candidates if c["vcodec"].lower().startswith("h264")
            or c["vcodec"].lower().startswith("avc")]
    rest = [c for c in candidates if c not in download and c not in h264]
    ordered = download + sorted(h264, key=lambda c: (c["height"], c["filesize"] or 0), reverse=True) \
        + sorted(rest, key=lambda c: (c["height"], c["filesize"] or 0), reverse=True)
    # De-duplicate identical URLs while keeping order.
    seen = set()
    out = []
    for cand in ordered:
        if cand["url"] not in seen:
            seen.add(cand["url"])
            out.append(cand)
    return out


def pick_direct(info: dict) -> dict | None:
    """First candidate of pick_direct_list, or None when no progressive MP4 exists."""
    found = pick_direct_list(info)
    return found[0] if found else None


def validate_stream_url(url: str) -> bool:
    """SSRF guard for a yt-dlp-provided media URL: public https host only.

    Rejects credentials, non-standard ports, and any hostname that resolves
    to a private/loopback/link-local/multicast/reserved address.
    """
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.username or parts.password:
            return False
        if parts.port not in (None, 443) or not parts.hostname:
            return False
        infos = socket.getaddrinfo(parts.hostname, 443, proto=socket.IPPROTO_TCP)
        if not infos:
            return False
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                return False
    except (ValueError, socket.gaierror, OSError):
        return False
    return True


def is_mp4_prefix(data: bytes) -> bool:
    return len(data) >= 8 and data[4:8] == b"ftyp"


def is_image_bytes(head: bytes, ext: str) -> bool:
    """Validate the first bytes match the claimed image format."""
    if ext in (".jpg", ".jpeg"):
        return head[:3] == b"\xff\xd8\xff"
    if ext == ".png":
        return head[:8] == b"\x89PNG\r\n\x1a\n"
    if ext == ".webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == ".gif":
        return head[:6] in (b"GIF87a", b"GIF89a")
    if ext == ".bmp":
        return head[:2] == b"BM"
    if ext == ".avif":
        return len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] == b"avif"
    return False


def _image_sort_key(path: Path) -> tuple[int, str]:
    """Sort downloaded images by their leading playlist index. A non-numeric
    prefix (e.g. 'NA' for a single image, whose playlist_index is empty)
    counts as 0."""
    prefix = path.name.split("_", 1)[0]
    try:
        return (int(prefix), path.name)
    except ValueError:
        return (0, path.name)


class _ValidatedRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to URLs that pass the same SSRF checks."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not validate_stream_url(newurl):
            raise urllib.error.URLError("redirect target failed validation")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_direct(url: str, headers: dict):
    """Open a validated direct media URL with yt-dlp's headers.

    Retained for a future direct-fetch fast path: the stream handler
    currently goes through the yt-dlp pipe instead, because TikTok's CDN
    refuses plain fetches from server IPs.
    """
    if not validate_stream_url(url):
        raise urllib.error.URLError("direct URL failed validation")
    safe = {k: str(v) for k, v in (headers or {}).items()
            if k.lower() not in ("host", "content-length", "connection")}
    opener = urllib.request.build_opener(_ValidatedRedirect)
    req = urllib.request.Request(url, headers=safe)
    return opener.open(req, timeout=STREAM_READ_TIMEOUT)


def open_ytdlp_pipe(url: str, raw_info: dict | None = None):
    """Let yt-dlp itself fetch the media and pipe it to stdout.

    Last-resort stream source: yt-dlp's own downloader is proven to work
    from this host even when a plain urllib fetch of the same CDN URL is
    refused. When the cached extraction (raw_info) is given, yt-dlp is fed
    it via --load-info-json and starts downloading immediately instead of
    extracting the post a second time (~10 s saved on the free tier).
    Returns (readable, close_fn); the caller validates the first bytes
    before sending any response headers.
    """
    info_path = None
    cmd = ytdlp_base()
    if isinstance(raw_info, dict) and raw_info.get("formats"):
        try:
            fd, info_path = tempfile.mkstemp(prefix="himtik-info-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(raw_info, handle)
            cmd += ["--load-info-json", info_path]
        except (OSError, ValueError, TypeError):
            info_path = None
    proc = subprocess.Popen(
        cmd + ["-f", "best[ext=mp4]/best", "--no-playlist", "-o", "-", url],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    def close_fn():
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        if info_path:
            try:
                os.unlink(info_path)
            except OSError:
                pass
    return proc.stdout, close_fn


def _download_images(job_id: str, url: str, folder: Path) -> None:
    """Download a photo post's images, one file per image (no zip).

    Each image is stored as 01.<ext>, 02.<ext>, ... in post order and listed
    in JOBS[job_id]["image_files"] for /api/jobs and /api/files to serve.
    Raises ValueError (caught by download_media) on any failure.
    """
    cmd = ytdlp_base(playlist=True) + [
        "--restrict-filenames", "--max-filesize", "120M",
        "--output", str(folder / "%(playlist_index)02d_%(id).80s.%(ext)s"),
    ]
    result = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=180, check=False)
    if result.returncode != 0:
        raise ValueError("Không thể tải nội dung này. Nguồn có thể bị giới hạn hoặc đã thay đổi.")
    found = [p for p in folder.iterdir() if p.is_file()]
    images = [p for p in found if p.suffix.lower() in IMAGE_EXTENSIONS]
    if not images:
        video_exts = (".mp4", ".mkv", ".webm")
        if any(p.suffix.lower() in video_exts for p in found):
            raise ValueError("Bài đăng này là video, hãy chọn loại Video.")
        raise ValueError("Không tìm thấy ảnh nào trong bài đăng này.")
    valid = []
    for p in images:
        with p.open("rb") as probe:
            head = probe.read(12)
        if is_image_bytes(head, p.suffix.lower()):
            valid.append(p)
    if not valid:
        raise ValueError("Không tìm thấy ảnh nào trong bài đăng này.")
    if len(valid) > MAX_IMAGES:
        raise ValueError(f"Album quá nhiều ảnh (tối đa {MAX_IMAGES} ảnh).")
    total = sum(p.stat().st_size for p in valid)
    if not 0 < total <= MAX_BYTES:
        raise ValueError("Tổng dung lượng ảnh vượt 120 MB.")
    valid.sort(key=_image_sort_key)
    renamed = []
    for i, p in enumerate(valid, 1):
        target = folder / f"{i:02d}{p.suffix.lower()}"
        if target != p:
            p.rename(target)
        renamed.append(target)
    with LOCK:
        JOBS[job_id].update(status="ready", bytes=total,
                            image_files=[str(p) for p in renamed])


def _gallery_platform(url: str) -> str | None:
    """'instagram' or 'facebook' when the link is an IG/FB post, else None.

    Only these two go through gallery-dl: yt-dlp cannot read their photo
    posts (IG: "No video formats found", FB: login wall), while TikTok and
    Douyin photo posts stay on the yt-dlp path (and TikTok images in the
    iPhone Shortcut stay on the TikWM fast path).
    """
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "instagram"
    if host == "facebook.com" or host.endswith(".facebook.com"):
        return "facebook"
    return None


def _gallery_cookie_file(platform: str) -> str | None:
    """Write the platform's optional cookie env var (Netscape format) to a
    temp file and return its path; None when the env var is unset/empty.

    The caller deletes the file after the gallery-dl run. The content is
    never logged and never written into the repo. IG_COOKIES / FB_COOKIES
    are set in the Render dashboard by the service owner, never pasted
    into chat. (tempfile.mkstemp creates the file with mode 0600.)
    """
    content = os.environ.get("IG_COOKIES" if platform == "instagram" else "FB_COOKIES", "")
    if not content.strip():
        return None
    fd, path = tempfile.mkstemp(prefix="himtik-cookies-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    return path


def _gallery_cmd(cookie_file: str | None) -> list[str]:
    """gallery-dl base command (argument list, never shell=True)."""
    cmd = ["gallery-dl"]
    if cookie_file:
        cmd += ["--cookies", cookie_file]
    return cmd


def _gallery_login_required(output: str) -> bool:
    """True when gallery-dl's output says the post needs a login."""
    text = output.lower()
    return any(marker in text for marker in _GALLERY_LOGIN_MARKERS)


def _gallery_login_error(cookie_file: str | None) -> str:
    """The login-required message: plain when no cookies are configured,
    expired-cookie hint when cookies were sent but still refused."""
    if cookie_file:
        return "Bài này cần đăng nhập, cookie có thể đã hết hạn."
    return "Bài này cần đăng nhập, chưa có cookie."


def _gallery_probe(url: str, cookie_file: str | None) -> int:
    """Count the media URLs gallery-dl finds for the post, without
    downloading. Raises ValueError (user-facing message) on login, on an
    unreadable post, or when nothing is found."""
    cmd = _gallery_cmd(cookie_file) + ["--get-urls", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=GALLERY_PROBE_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise ValueError("Nguồn phản hồi quá chậm. Vui lòng thử lại.")
    except OSError:
        raise ValueError("Máy chủ chưa cài gallery-dl.")
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    if _gallery_login_required(output):
        raise ValueError(_gallery_login_error(cookie_file))
    if result.returncode != 0:
        raise ValueError("Không đọc được bài đăng. Hãy thử liên kết công khai khác.")
    urls = [line.strip() for line in (result.stdout or "").splitlines()
            if line.strip().startswith("http")]
    if not urls:
        raise ValueError("Không tìm thấy ảnh nào trong bài đăng này.")
    return len(urls)


_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE)
_OG_IMAGE_RE_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    re.IGNORECASE)
_OG_IMAGE_SECURE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image:secure_url["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE)
_OG_VIDEO_RE = re.compile(
    r'<meta[^>]+property=["\']og:video["\']', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_USER_ID_RE = re.compile(r'"USER_ID":"([^"]*)"')
# A Facebook CDN image URL (scontent/fbcdn host). The query string is
# kept for the download URL; only host+path is shown in debug output.
_FB_CDN_URL_RE = re.compile(r'https?://[A-Za-z0-9.-]*(?:scontent|fbcdn)[A-Za-z0-9.-]*\.[a-z]+/[^"\'\s\\]+')
# Photo fbids attached to a post page (the attached photos' nodes).
_PHOTO_FBID_RE = re.compile(r'"__typename":"Photo","id":"(\d+)"')
# Width/height near an image URL in the page's JSON.
_JSON_IMAGE_DIMS_RE = re.compile(r'"(width|height)":(\d+)')
# Image URLs to skip in a Facebook page's JSON (2.0.12): profile pictures,
# avatars, reaction/emoji/sticker images, ads and suggested content. The
# check runs on the lowercased JSON around the URL.
_FB_IMAGE_BLOCKLIST = ("profile_pic", "profile_picture", "avatar", "reaction",
                       "emoji", "sticker", "sponsored", "is_ad", "advertis",
                       "suggested")


def _facebook_unescape(page_html: str) -> str:
    """Unescape a Facebook page's JSON escapes (\/, \\u0026) and HTML
    entities (&amp;) so image URLs become plain https://... links. Used
    only for searching the page for image URLs, never returned."""
    return html.unescape(page_html.replace("\\/", "/").replace("\\u0026", "&"))


def _facebook_target_fbid(url: str) -> str | None:
    """The fbid query param of a Facebook photo URL, or None (a post URL
    has no fbid)."""
    values = parse_qs(urlsplit(url).query).get("fbid")
    return values[0] if values else None


def _facebook_fbid_in(text: str, fbid: str) -> bool:
    """Whether fbid appears in text as a standalone number (not inside a
    longer number, so a short fbid never matches inside e.g. 1080)."""
    return re.search(r"(?<!\d)" + re.escape(fbid) + r"(?!\d)", text) is not None


def _facebook_best_image_url(page_html: str, target_fbid: str | None) -> str | None:
    """The main photo URL from a Facebook page's embedded JSON. The page
    is unescaped first (the links are escaped as \\/ and \\u0026, so a
    plain 'https://scontent...' search finds nothing), then every
    scontent/fbcdn image URL is scored: the URL closest to the target
    fbid (same JSON block) wins; when no URL sits near the fbid, the
    largest by width x height wins. Avatars, ads, reaction/emoji/
    sticker and suggested images are skipped. Returns None when the page
    has no usable photo URL."""
    text = _facebook_unescape(page_html)
    # Positions of the target fbid as a standalone number. The photo's
    # own image URL sits in the same JSON block as its fbid, so the URL
    # closest to an fbid occurrence is the target photo.
    fbid_positions = []
    if target_fbid:
        fbid_positions = [m.start() for m in re.finditer(
            r"(?<!\d)" + re.escape(target_fbid) + r"(?!\d)", text)]
    best_url = None
    best_key = None  # (fbid-nearby first, then closest, then largest)
    for match in _FB_CDN_URL_RE.finditer(text):
        url = match.group(0)
        parts = urlsplit(url)
        if "scontent" not in parts.netloc and "fbcdn" not in parts.netloc:
            continue
        # The blocklist runs on a tight scope — the JSON keys in the ~80
        # chars before the URL (the image's own parent key, e.g.
        # "profile_pic") and the object's own fields right after it. A
        # wide window, or the raw text before the URL, would leak a
        # blocklisted word from a neighbouring image (e.g. the previous
        # image's filename "avatar.jpg") into this one.
        before = text[max(0, match.start() - 80):match.start()]
        after = text[match.end():match.end() + 200]
        # Only the dims of THIS image: stop at the next URL, so a later
        # (larger) image's dimensions never leak into this one.
        nxt = re.search(r'https?://', after)
        if nxt:
            after = after[:nxt.start()]
        before_keys = [k.lower() for k in re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:', before)]
        if any(blocked in key for key in before_keys for blocked in _FB_IMAGE_BLOCKLIST):
            continue
        if any(blocked in after.lower() for blocked in _FB_IMAGE_BLOCKLIST):
            continue
        dims = dict(_JSON_IMAGE_DIMS_RE.findall(after))
        area = 0
        if "width" in dims and "height" in dims:
            area = int(dims["width"]) * int(dims["height"])
        if fbid_positions:
            dist = min(abs(match.start() - pos) for pos in fbid_positions)
            key = (0, -dist, area) if dist <= 1000 else (1, 0, area)
        else:
            key = (1, 0, area)
        if best_key is None or key > best_key:
            best_key = key
            best_url = url
    return best_url


def _facebook_post_photo_fbids(page_html: str) -> list[str]:
    """Photo fbids attached to a Facebook post page (the attached photos'
    nodes: "__typename":"Photo","id":"..."). Deduplicated, capped at
    MAX_GALLERY_IMAGES."""
    ids = []
    for match in _PHOTO_FBID_RE.finditer(page_html):
        if match.group(1) not in ids:
            ids.append(match.group(1))
    return ids[:MAX_GALLERY_IMAGES]


def _facebook_debug_info(page_html: str, target_fbid: str | None) -> str:
    """Debug info for a Facebook parser failure. Only included when the
    HIMTIK_FB_DEBUG env var is set. Never includes cookie values or raw
    HTML — only scontent counts (raw vs unescaped), up to 10 image-link
    samples (the JSON key right before each link, host+path with the
    query string stripped, nearby dimensions), and whether the target
    fbid is in the page."""
    raw_count = page_html.count("scontent")
    text = _facebook_unescape(page_html)
    unesc_count = text.count("scontent")
    samples = []
    for match in _FB_CDN_URL_RE.finditer(text):
        if len(samples) >= 10:
            break
        url = match.group(0)
        parts = urlsplit(url)
        host_path = parts.netloc + parts.path
        before = text[max(0, match.start() - 120):match.start()]
        key_match = re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:', before)
        key = key_match[-1] if key_match else "?"
        after = text[match.end():match.end() + 200]
        nxt = re.search(r'https?://', after)
        if nxt:
            after = after[:nxt.start()]
        dims = dict(_JSON_IMAGE_DIMS_RE.findall(after))
        dim_text = ""
        if "width" in dims and "height" in dims:
            dim_text = f" {dims['width']}x{dims['height']}"
        samples.append(f"{key}: {host_path}{dim_text}")
    if target_fbid:
        fbid_text = f"fbid {'có' if _facebook_fbid_in(text, target_fbid) else 'không'} trong trang"
    else:
        fbid_text = "không có fbid đích"
    return (
        f"[Debug: scontent trong HTML thô: {raw_count}, sau unescape: {unesc_count}; "
        f"{fbid_text}; mẫu ({len(samples)}): " + "; ".join(samples) + "]"
    )


def _cookie_value(header: str, name: str) -> str | None:
    """One cookie's value from a Cookie header, or None. The value is only
    ever used for an in-memory comparison and is never returned in a
    diagnostic or an error message."""
    for part in header.split(";"):
        part = part.strip()
        if part.startswith(name + "="):
            return part[len(name) + 1:]
    return None


def _cookie_header_info(header: str) -> dict:
    """Names, duplicates, c_user/xs presence and length of a Cookie header.
    Cookie values are never returned."""
    names = []
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        names.append(part.split("=", 1)[0])
    duplicates = sorted({name for name in names if names.count(name) > 1})
    return {
        "names": names,
        "duplicates": duplicates,
        "has_c_user": "c_user" in names,
        "has_xs": "xs" in names,
        "length": len(header),
    }


def _user_id_info(page_html: str, c_user_value: str | None) -> tuple[bool, bool]:
    """(has_user_id, matches_c_user) for a Facebook page. A logged-out
    page has no USER_ID field at all (verified 2026-11-19: the logged-out
    error/login pages contain zero USER_ID occurrences), so absence means
    the page is a shell/error page, not a real logged-in app page. The
    c_user value is compared in memory only and never reported."""
    match = _USER_ID_RE.search(page_html)
    if not match:
        return False, False
    if c_user_value is None:
        return True, False
    return True, match.group(1) == c_user_value


def _facebook_diagnostic(final_url: str, status: int, body_len: int,
                         cookie_header: str, page_html: str) -> str:
    """One-line diagnostic for a Facebook parser failure. Never includes
    cookie values or page content — only the final URL path (after
    redirects), the HTTP status, the body length, the cookie names sent
    (c_user/xs presence, duplicates, header length), and USER_ID presence
    plus whether it matches c_user (true/false only)."""
    info = _cookie_header_info(cookie_header)
    has_uid, uid_matches = _user_id_info(page_html, _cookie_value(cookie_header, "c_user"))
    final_path = urlsplit(final_url).path or "/"
    status_text = str(status) if status else "không có phản hồi"
    return (
        f"[Chẩn đoán: URL cuối: {final_path}, HTTP {status_text}, "
        f"body {body_len} B, cookie: {'c_user có' if info['has_c_user'] else 'c_user không'}, "
        f"{'xs có' if info['has_xs'] else 'xs không'}, "
        f"tên: {', '.join(info['names']) or 'không có'}, "
        f"trùng: {', '.join(info['duplicates']) or 'không'}, "
        f"dài {info['length']} ký tự, "
        f"USER_ID: {'có' if has_uid else 'không'}, khớp c_user: {'đúng' if uid_matches else 'sai'}]"
    )


def _page_title(page_html: str) -> str:
    """The page <title>, cleaned and truncated; "" when absent. Used only
    to say what kind of page Facebook served when og:image is missing."""
    match = _TITLE_RE.search(page_html)
    if not match:
        return ""
    title = html.unescape(match.group(1)).strip()
    return re.sub(r"\s+", " ", title)[:60]


def _facebook_og_image_url(page_html: str) -> str | None:
    """The og:image URL of a Facebook photo page, or None when absent.
    Falls back to og:image:secure_url when og:image itself is missing."""
    for pattern in (_OG_IMAGE_RE, _OG_IMAGE_RE_REV, _OG_IMAGE_SECURE_RE):
        match = pattern.search(page_html)
        if match:
            url = html.unescape(match.group(1)).strip()
            if url.startswith("http"):
                return url
    return None


def _image_ext_from_url_or_bytes(url: str, head: bytes) -> str:
    """File extension for a downloaded image: the URL suffix first,
    otherwise sniff the magic bytes."""
    ext = Path(urlsplit(url).path).suffix.lower()
    if ext in IMAGE_EXTENSIONS and is_image_bytes(head, ext):
        return ext
    for candidate in (".jpg", ".png", ".webp", ".gif", ".bmp", ".avif"):
        if is_image_bytes(head, candidate):
            return candidate
    return ".jpg"


def _netscape_cookie_header(cookie_file: str, url: str) -> str:
    """Build a Cookie header from a Netscape-format cookie file, leniently.

    Cookie-Editor exports leading-dot domains (".facebook.com") with the
    domain flag FALSE; CPython's MozillaCookieJar asserts the two agree and
    refuses the whole file (LoadError), so the file is read by hand and
    bad lines are skipped instead of failing. Only cookies matching the
    request host and path are sent. Returns "" when nothing usable is found.
    """
    host = (urlsplit(url).hostname or "").lower()
    path = urlsplit(url).path or "/"
    try:
        with open(cookie_file, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return ""
    pairs = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            fields = line.split()
        if len(fields) != 7:
            continue
        domain, _flag, cookie_path, _secure, _expires, name, value = fields
        domain = domain.lower().lstrip(".")
        if not (host == domain or host.endswith("." + domain)):
            continue
        if not path.startswith(cookie_path):
            continue
        if not name or "=" in name:
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _fetch_facebook_page(url: str, headers: dict, cookie_header: str):
    """Fetch one Facebook page with the parser headers and cookies.
    Returns (page, final_url, status, raw_len). Raises ValueError (with
    the diagnostic line) on any failure."""
    page_request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(page_request, timeout=FB_PARSER_PAGE_TIMEOUT) as resp:
            final_url = resp.geturl()
            status = resp.status
            raw = resp.read()
            page = raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            err_raw = exc.read()
        except Exception:
            err_raw = b""
        err_page = err_raw.decode("utf-8", "replace")
        diag = _facebook_diagnostic(exc.geturl(), exc.code, len(err_raw),
                                    cookie_header, err_page)
        raise ValueError(f"Không đọc được bài đăng (HTTP {exc.code}). Hãy thử liên kết công khai khác. {diag}")
    except (urllib.error.URLError, OSError):
        diag = _facebook_diagnostic(url, 0, 0, cookie_header, "")
        raise ValueError(f"Không đọc được bài đăng. Hãy thử liên kết công khai khác. {diag}")
    return page, final_url, status, len(raw)


def _fetch_facebook_image(image_url: str, headers: dict) -> tuple[bytes, str]:
    """Download one Facebook image URL. Returns (data, ext). Raises
    ValueError when the download fails, the file is too big, or the
    content is not an image."""
    image_request = urllib.request.Request(image_url, headers=headers)
    try:
        with urllib.request.urlopen(image_request, timeout=FB_PARSER_IMAGE_TIMEOUT) as resp:
            data = resp.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError):
        raise ValueError("Không tải được ảnh từ bài đăng này.")
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Tổng dung lượng ảnh vượt 120 MB.")
    ext = _image_ext_from_url_or_bytes(image_url, data[:12])
    if not is_image_bytes(data[:12], ext):
        raise ValueError("Không tìm thấy ảnh nào trong bài đăng này.")
    return data, ext


def _raise_facebook_no_image(page: str, final_url: str, status: int, raw_len: int,
                             cookie_header: str, cookie_file: str | None,
                             target_fbid: str | None) -> None:
    """Raise the 'no photo found' error for a Facebook page. Includes the
    page title and the diagnostic line; when HIMTIK_FB_DEBUG is set, also
    the debug line (counts and samples only — never cookie values or raw
    HTML)."""
    if _gallery_login_required(page):
        raise ValueError(_gallery_login_error(cookie_file))
    title = _page_title(page)
    detail = f" (Trang: {title})" if title else ""
    diag = _facebook_diagnostic(final_url, status, raw_len, cookie_header, page)
    debug = ""
    if os.environ.get("HIMTIK_FB_DEBUG"):
        debug = " " + _facebook_debug_info(page, target_fbid)
    raise ValueError(f"Không tìm thấy ảnh nào trong bài đăng này.{detail} {diag}{debug}")


def _download_images_facebook_parser(job_id: str, url: str, folder: Path,
                                     cookie_file: str) -> None:
    """Facebook photo fallback: fetch the post page with the FB cookies
    and take its photo(s). gallery-dl's Facebook extractor cannot parse
    some photo pages (it crashes, e.g. KeyError 'fbid'), so this bypasses
    it. A photo URL (fbid=...) returns its single photo; a post URL
    (/posts/...) fetches each attached photo's own page for the original.
    Video posts are rejected instead of returning a thumbnail. Raises
    ValueError (user-facing message) on any failure."""
    headers = dict(FB_PARSER_HEADERS)
    cookie_header = _netscape_cookie_header(cookie_file, url)
    if cookie_header:
        headers["Cookie"] = cookie_header
    target_fbid = _facebook_target_fbid(url)
    page, final_url, status, raw_len = _fetch_facebook_page(url, headers, cookie_header)
    if _OG_VIDEO_RE.search(page):
        raise ValueError("Bài đăng này là video, hãy chọn loại Video.")
    if target_fbid:
        image_url = _facebook_og_image_url(page) or _facebook_best_image_url(page, target_fbid)
        if not image_url:
            _raise_facebook_no_image(page, final_url, status, raw_len,
                                     cookie_header, cookie_file, target_fbid)
        data, ext = _fetch_facebook_image(image_url, headers)
        target = folder / f"01{ext}"
        target.write_bytes(data)
        with LOCK:
            JOBS[job_id].update(status="ready", bytes=len(data), found=1, downloaded=1,
                                image_files=[str(target)])
        return
    # A post URL: collect the attached photos' fbids, then fetch each
    # photo's own page for the original image.
    fbids = _facebook_post_photo_fbids(page)
    if not fbids:
        _raise_facebook_no_image(page, final_url, status, raw_len,
                                 cookie_header, cookie_file, None)
    image_files = []
    total_bytes = 0
    for index, fbid in enumerate(fbids, start=1):
        photo_url = f"https://www.facebook.com/photo.php?fbid={fbid}"
        try:
            photo_page, _, _, _ = _fetch_facebook_page(photo_url, headers, cookie_header)
        except ValueError:
            continue
        if _OG_VIDEO_RE.search(photo_page):
            continue
        image_url = (_facebook_og_image_url(photo_page)
                     or _facebook_best_image_url(photo_page, fbid))
        if not image_url:
            continue
        try:
            data, ext = _fetch_facebook_image(image_url, headers)
        except ValueError:
            continue
        target = folder / f"{index:02d}{ext}"
        target.write_bytes(data)
        image_files.append(str(target))
        total_bytes += len(data)
    if not image_files:
        _raise_facebook_no_image(page, final_url, status, raw_len,
                                 cookie_header, cookie_file, None)
    with LOCK:
        job = JOBS[job_id]
        job.update(status="ready", bytes=total_bytes, found=len(fbids),
                   downloaded=len(image_files), image_files=image_files)
        if len(image_files) < len(fbids):
            job["warning"] = f"Chỉ tải được {len(image_files)}/{len(fbids)} ảnh."


def _download_images_gallery(job_id: str, url: str, folder: Path) -> None:
    """Instagram/Facebook photo posts via gallery-dl.

    Probe first for the found count, then download into the job folder.
    Every image is served as an individual file (no zip), so the iPhone
    Shortcut can save each straight into Photos. A partial download is
    still returned, with found/downloaded/warning on the job.
    For Facebook, when gallery-dl cannot parse the photo page, the server
    falls back to the og:image parser using the FB cookies.
    Raises ValueError (caught by download_media) on any failure.
    """
    platform = _gallery_platform(url) or "instagram"
    cookie_file = _gallery_cookie_file(platform)
    try:
        try:
            found = _gallery_probe(url, cookie_file)
            if found > MAX_GALLERY_IMAGES:
                raise ValueError(f"Album quá nhiều ảnh (tối đa {MAX_GALLERY_IMAGES} ảnh).")
            cmd = _gallery_cmd(cookie_file) + ["-d", str(folder), url]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        timeout=GALLERY_DOWNLOAD_TIMEOUT, check=False)
            except subprocess.TimeoutExpired:
                raise ValueError("Quá thời gian xử lý. Hãy thử album ít ảnh hơn.")
            except OSError:
                raise ValueError("Máy chủ chưa cài gallery-dl.")
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            if _gallery_login_required(output):
                raise ValueError(_gallery_login_error(cookie_file))
            if result.returncode != 0:
                raise ValueError("Không tải được ảnh từ bài đăng này.")
            # gallery-dl writes into extractor subfolders; collect recursively.
            found_files = [p for p in sorted(folder.rglob("*")) if p.is_file()]
            images = [p for p in found_files if p.suffix.lower() in IMAGE_EXTENSIONS]
            valid = []
            for p in images:
                with p.open("rb") as probe:
                    head = probe.read(12)
                if is_image_bytes(head, p.suffix.lower()):
                    valid.append(p)
            if not valid:
                video_exts = (".mp4", ".mkv", ".webm", ".mov")
                if any(p.suffix.lower() in video_exts for p in found_files):
                    raise ValueError("Bài đăng này là video, hãy chọn loại Video.")
                raise ValueError("Không tìm thấy ảnh nào trong bài đăng này.")
            downloaded = len(valid)
            if downloaded > MAX_GALLERY_IMAGES:
                # The probe already capped this; a post can gain items between
                # probe and download, so check again here.
                raise ValueError(f"Album quá nhiều ảnh (tối đa {MAX_GALLERY_IMAGES} ảnh).")
            total = sum(p.stat().st_size for p in valid)
            if not 0 < total <= MAX_BYTES:
                raise ValueError("Tổng dung lượng ảnh vượt 120 MB.")
            # Flatten to 01.<ext>, 02.<ext>, ... in post order. Two-phase
            # rename: a file may already carry a target name.
            staged = []
            for i, p in enumerate(valid, 1):
                temp = folder / f".g{i:02d}{p.suffix.lower()}"
                p.rename(temp)
                staged.append(temp)
            renamed = []
            for i, temp in enumerate(staged, 1):
                target = folder / f"{i:02d}{temp.suffix.lower()}"
                temp.rename(target)
                renamed.append(str(target))
            warning = ""
            if downloaded < found:
                warning = f"Chỉ tải được {downloaded}/{found} ảnh."
            with LOCK:
                job = JOBS[job_id]
                job.update(status="ready", bytes=total, found=found, downloaded=downloaded)
                if warning:
                    job["warning"] = warning
                job["image_files"] = renamed
        except ValueError as exc:
            # Facebook fallback: gallery-dl's extractor cannot parse some
            # photo pages. Try the og:image parser when FB cookies are
            # configured; login/cap/size/video failures are re-raised as-is.
            # The parser's own error propagates (it is the most specific
            # one: it says whether the page, the og:image, or the image
            # download failed) instead of the generic gallery-dl error.
            message = str(exc)
            if (platform == "facebook" and cookie_file
                    and not message.startswith(_FB_PARSER_SKIP_PREFIXES)):
                _download_images_facebook_parser(job_id, url, folder, cookie_file)
                return
            raise
    finally:
        if cookie_file:
            try:
                os.unlink(cookie_file)
            except OSError:
                pass


def download_media(job_id: str, url: str, file_type: str) -> None:
    folder = STORAGE / job_id
    try:
        with LOCK:
            JOBS[job_id]["status"] = "processing"
        folder.mkdir(parents=True, exist_ok=True)
        if file_type == "image":
            if _gallery_platform(url) is not None:
                _download_images_gallery(job_id, url, folder)
            else:
                _download_images(job_id, url, folder)
            return
        cmd = ytdlp_base() + [
            "--restrict-filenames", "--max-filesize", "120M",
            "--output", str(folder / "%(id).80s.%(ext)s"),
        ]
        if file_type == "audio":
            cmd += ["-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "192K"]
        else:
            cmd += ["-f", "best[ext=mp4]/best", "--merge-output-format", "mp4"]
        result = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=180, check=False)
        if result.returncode != 0:
            raise ValueError("Không thể tải nội dung này. Nguồn có thể bị giới hạn hoặc đã thay đổi.")
        extensions = (".mp3",) if file_type == "audio" else (".mp4", ".mkv", ".webm")
        available = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in extensions]
        if not available:
            raise ValueError("Không tìm thấy tệp đầu ra tương thích.")
        selected = max(available, key=lambda p: p.stat().st_size)
        size = selected.stat().st_size
        if not 0 < size <= MAX_BYTES:
            raise ValueError("Tệp quá lớn; giới hạn hiện tại là 120 MB.")
        # Format check: the downloaded bytes must match the requested container.
        with selected.open("rb") as probe:
            head = probe.read(12)
        if file_type == "audio":
            ok = head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
        else:
            ok = is_mp4_prefix(head) or head[:4] in (b"\x1a\x45\xdf\xa3",)  # mp4 or mkv/webm
        if not ok:
            raise ValueError("Tệp tải về không đúng định dạng.")
        with LOCK:
            JOBS[job_id].update(status="ready", filename=str(selected), bytes=size)
    except (ValueError, subprocess.TimeoutExpired, OSError) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            message = "Quá thời gian xử lý. Hãy thử video ngắn hơn."
        elif isinstance(exc, FileNotFoundError):
            message = "Máy chủ chưa cài yt-dlp hoặc FFmpeg."
        elif isinstance(exc, ValueError):
            message = str(exc)
        else:
            message = "Xử lý thất bại. Vui lòng thử lại."
        with LOCK:
            if job_id in JOBS:
                JOBS[job_id].update(status="error", error=message)
        shutil.rmtree(folder, ignore_errors=True)


def cleanup_jobs() -> None:
    now = time.time()
    with LOCK:
        expired = [jid for jid, j in JOBS.items() if now - j["created"] > JOB_TTL_SECONDS and j["status"] not in ("queued", "processing")]
        for jid in expired:
            JOBS.pop(jid, None)
        for ip in list(RATE):
            RATE[ip] = [v for v in RATE[ip] if now - v < 600]
            if not RATE[ip]:
                del RATE[ip]
    for jid in expired:
        shutil.rmtree(STORAGE / jid, ignore_errors=True)


def check_rate(ip: str, weight: int = 1) -> bool:
    # Rate limiting DISABLED at Him's request (2026-11-09): nobody else
    # uses this service, so the 429 limit only got in his way. The call
    # site is kept (and the 501-safe body-read order with it) so the limit
    # can be restored by putting the check back here; the regression tests
    # still exercise the 429 path by mocking this function.
    return True


class App(BaseHTTPRequestHandler):
    # Note: no protocol_version override, so this speaks HTTP/1.0 and every
    # response closes its connection. That is what makes a rejected POST
    # harmless: unread request bytes can never leak into a following request
    # on a reused socket (the HTTP 501 class of bug fixed in V3.0.1).
    server_version = "HimTik/2.0.12"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Don't log TikTok links or identifying query strings.
        print("[http]", self.command, self.path.split("?")[0], self.address_string(), flush=True)

    def cors_headers(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def headers_common(self):
        self.cors_headers()
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

    def json_response(self, code: int, data: dict):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        # An early POST rejection can leave unread request bytes. HTTP/1.1 must
        # explicitly close that connection rather than parsing JSON as a method.
        if self.close_connection:
            self.send_header("Connection", "close")
        self.headers_common()
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        cleanup_jobs()
        route = urlsplit(self.path).path
        if route == "/api/health":
            return self.json_response(200, {"ok": True, "service": "HimTik"})
        if route.startswith("/api/jobs/"):
            jid = route.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(jid)
                payload = {k: job[k] for k in ("status", "error", "bytes", "found", "downloaded", "warning") if k in job} if job else None
                mode = job.get("mode") if job else None
                image_files = list(job.get("image_files") or []) if job else []
            if payload is None:
                return self.json_response(404, {"error": "Phiên tải không tồn tại hoặc đã hết hạn."})
            if payload["status"] == "ready":
                if mode == "stream":
                    payload["mode"] = "stream"
                    payload["stream_url"] = "/api/stream/" + jid
                elif image_files:
                    files = []
                    for i, p in enumerate(image_files):
                        path = Path(p)
                        try:
                            size = path.stat().st_size
                        except OSError:
                            size = 0
                        files.append({"name": path.name, "url": f"/api/files/{jid}/{i}", "bytes": size})
                    payload["files"] = files
                else:
                    payload["download_url"] = "/api/files/" + jid
            return self.json_response(200, payload)
        if route.startswith("/api/stream/"):
            jid = route.rsplit("/", 1)[-1]
            return self.handle_stream(jid)
        if route.startswith("/api/files/"):
            rest = route[len("/api/files/"):]
            parts = rest.split("/")
            jid = parts[0]
            index = parts[1] if len(parts) > 1 else None
            with LOCK:
                job = JOBS.get(jid)
                image_files = list(job.get("image_files") or []) if job else []
                filename = job.get("filename") if job and job["status"] == "ready" else None
            if index is not None:
                # Image album: index is digits only and used as a list index
                # into job["image_files"]; never joined into a file path.
                if not index.isdigit():
                    return self.json_response(404, {"error": "Tệp chưa sẵn sàng hoặc đã hết hạn."})
                idx = int(index)
                if not job or job.get("status") != "ready" or idx >= len(image_files):
                    return self.json_response(404, {"error": "Tệp chưa sẵn sàng hoặc đã hết hạn."})
                pathname = image_files[idx]
                is_image = True
            else:
                if not filename:
                    return self.json_response(404, {"error": "Tệp chưa sẵn sàng hoặc đã hết hạn."})
                pathname = filename
                is_image = False
            path = Path(pathname)
            if not path.is_file() or path.parent != STORAGE / jid:
                return self.json_response(404, {"error": "Tệp tải không còn tồn tại."})
            suffix = path.suffix.lower()
            if is_image:
                file_type = IMAGE_CONTENT_TYPES.get(suffix) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                name = path.name
                disposition = "inline"
            else:
                file_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                name = "HimTik-" + jid[:8] + suffix
                disposition = "attachment"
            self.send_response(200)
            self.send_header("Content-Type", file_type)
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'{disposition}; filename="{name}"')
            self.headers_common()
            self.end_headers()
            try:
                with path.open("rb") as f:
                    shutil.copyfileobj(f, self.wfile, length=256 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if route == "/":
            route = "/index.html"
        # Only allow the known web assets; never serve server.py, job files, etc.
        if route not in ("/index.html", "/style.css", "/app.js", "/favicon.svg"):
            return self.json_response(404, {"error": "Không tìm thấy đường dẫn."})
        path = PUBLIC / unquote(route[1:])
        if not path.is_file():
            return self.json_response(404, {"error": "Thiếu tệp giao diện."})
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.headers_common()
        self.end_headers()
        self.wfile.write(data)

    def handle_stream(self, jid):
        """Pipe a stream job's video to the client without saving it.

        Plain fetches of TikTok's CDN URLs are refused from server IPs
        (verified repeatedly), so no direct-URL attempt is made here: the
        stream goes straight to yt-dlp, fed the cached extraction via
        --load-info-json, so the post is extracted once per link instead
        of twice. The first bytes (MP4 magic) are validated before any
        response headers are sent, so a bad source still returns a JSON
        error. If the pipe fails the client is told to use the
        full-download fallback.
        """
        with LOCK:
            job = JOBS.get(jid)
            if not job or job.get("mode") != "stream":
                return self.json_response(404, {"error": "Phiên truyền trực tiếp không tồn tại hoặc đã hết hạn."})
            source_url = job.get("source_url") or ""
        started = time.monotonic()
        source = None          # object with .read(n)
        close_source = None    # callable to release it
        first = b""
        last_error = ""

        def try_pipe():
            nonlocal first
            raw = raw_info_for(source_url)
            close_fn = None
            try:
                stream, close_fn = open_ytdlp_pipe(source_url, raw)
                ready, _, _ = select.select([stream], [], [], 60)
                if not ready:
                    raise TimeoutError("yt-dlp pipe produced no data in time")
                chunk = stream.read(32 * 1024)
                if not chunk:
                    raise ValueError("yt-dlp pipe produced no data")
                if not is_mp4_prefix(chunk):
                    raise ValueError("yt-dlp pipe did not produce MP4 data")
                first = chunk
                return stream, close_fn
            except Exception:
                if close_fn is not None:
                    try:
                        close_fn()
                    except Exception:
                        pass
                raise

        if source_url:
            try:
                source, close_source = try_pipe()
                print("[timing] stream yt-dlp pipe ok", flush=True)
            except Exception as exc:  # noqa: BLE001 - pipe failure means fall back
                last_error = type(exc).__name__
                print(f"[timing] stream pipe failed: {last_error}", flush=True)
                source = None
                first = b""
        if source is None:
            print(f"[timing] stream failed: {last_error}", flush=True)
            return self.json_response(502, {"error": "Không truyền trực tiếp được. Hãy dùng “Tải bản đầy đủ”.", "fallback": "full"})
        ttfb = time.monotonic() - started
        name = "HimTik-" + jid[:8] + ".mp4"
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Disposition", f'inline; filename="{name}"')
        self.headers_common()
        self.end_headers()
        sent = 0
        try:
            self.wfile.write(first)
            sent += len(first)
            while True:
                if time.monotonic() - started > STREAM_DEADLINE_SECONDS:
                    break
                chunk = source.read(STREAM_CHUNK)
                if not chunk:
                    break
                sent += len(chunk)
                if sent > MAX_BYTES:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                if close_source is not None:
                    close_source()
            except Exception:
                pass
        print(f"[timing] stream ttfb={ttfb:.1f}s total={time.monotonic() - started:.1f}s bytes={sent}", flush=True)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        cleanup_jobs()
        route = urlsplit(self.path).path
        if route not in ("/api/inspect", "/api/download"):
            # Route rejection happens before the body is read: never reuse
            # this TCP connection with leftover JSON bytes.
            self.close_connection = True
            return self.json_response(404, {"error": "Đường dẫn không hợp lệ."})
        if self.headers.get("Transfer-Encoding"):
            # This simple server supports bounded Content-Length, not chunked
            # request bodies. Close to prevent parsing leftover chunks as HTTP.
            self.close_connection = True
            return self.json_response(400, {"error": "Không hỗ trợ yêu cầu dạng chunked."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self.close_connection = True
            return self.json_response(400, {"error": "Content-Length không hợp lệ."})
        if length <= 0 or length > MAX_REQUEST:
            # Do not read an unbounded body: reject and close the connection.
            self.close_connection = True
            return self.json_response(413 if length > MAX_REQUEST else 400,
                                      {"error": "Dữ liệu yêu cầu quá lớn hoặc bị thiếu."})
        try:
            body = self.rfile.read(length)
        except (OSError, TimeoutError):
            self.close_connection = True
            return self.json_response(400, {"error": "Không đọc được dữ liệu yêu cầu."})
        if len(body) != length:
            self.close_connection = True
            return self.json_response(400, {"error": "Dữ liệu yêu cầu không đầy đủ."})
        # Read the complete bounded request body BEFORE applying rate limits.
        # Without this, a rejected POST leaves {json} in the socket and a
        # following GET becomes '{json}GET' (HTTP 501/400).
        ip = self.client_address[0]  # Don't trust X-Forwarded-For from arbitrary clients.
        if not check_rate(ip, 3 if route == "/api/download" else 1):
            return self.json_response(429, {"error": "Thao tác quá nhanh. Vui lòng thử lại sau ít phút."})
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("Dữ liệu yêu cầu không hợp lệ.")
            url = extract_url(payload.get("url", ""))
        except (ValueError, TypeError) as exc:
            return self.json_response(400, {"error": str(exc) or "Dữ liệu không hợp lệ."})
        if route == "/api/inspect":
            inspect_type = payload.get("type", "video")
            try:
                return self.json_response(200, public_info(inspect_media(url, allow_playlist=(inspect_type == "image"))))
            except subprocess.TimeoutExpired:
                return self.json_response(504, {"error": "Nguồn phản hồi quá chậm. Vui lòng thử lại."})
            except ValueError as exc:
                return self.json_response(422, {"error": str(exc)})
            except OSError:
                return self.json_response(422, {"error": "Máy chủ chưa cài yt-dlp hoặc FFmpeg."})
        file_type = payload.get("type", "video")
        if file_type not in ("video", "audio", "image"):
            return self.json_response(400, {"error": "Định dạng không hỗ trợ."})
        mode = payload.get("mode", "stream")
        if file_type == "video" and mode != "full":
            # Fast path: resolve the direct MP4 once (cached) and hand the
            # client a ready stream job. No file is downloaded to Render.
            try:
                info = inspect_media(url)
                direct_list = pick_direct_list(info)
                direct = direct_list[0] if direct_list else None
            except subprocess.TimeoutExpired:
                return self.json_response(504, {"error": "Nguồn phản hồi quá chậm. Vui lòng thử lại."})
            except ValueError as exc:
                return self.json_response(422, {"error": str(exc)})
            except OSError:
                return self.json_response(422, {"error": "Máy chủ chưa cài yt-dlp hoặc FFmpeg."})
            if direct:
                jid = secrets.token_urlsafe(24)
                with LOCK:
                    JOBS[jid] = {"created": time.time(), "status": "ready", "error": "",
                                 "bytes": direct["filesize"] or 0, "mode": "stream",
                                 "direct": direct, "direct_list": direct_list,
                                 "source_url": url}
                print(f"[timing] download stream-ready format={direct['format_id']} bytes={direct['filesize']} candidates={len(direct_list)}", flush=True)
                return self.json_response(202, {"job_id": jid, "status": "ready",
                                                "mode": "stream", "stream_url": "/api/stream/" + jid})
        if file_type == "image" and _gallery_platform(url) is None:
            # Pre-check: refuse video posts before downloading up to 120 MB.
            # Instagram/Facebook skip this: yt-dlp cannot read their photo
            # posts at all, so the gallery-dl worker does the video/login
            # detection instead and reports it on the job.
            try:
                info = inspect_media(url, allow_playlist=True)
            except subprocess.TimeoutExpired:
                return self.json_response(504, {"error": "Nguồn phản hồi quá chậm. Vui lòng thử lại."})
            except ValueError as exc:
                return self.json_response(422, {"error": str(exc)})
            except OSError:
                return self.json_response(422, {"error": "Máy chủ chưa cài yt-dlp hoặc FFmpeg."})
            if info.get("_type") not in ("playlist", "multi_video"):
                if any(isinstance(f, dict) and f.get("vcodec") not in (None, "none")
                       for f in (info.get("formats") or [])):
                    return self.json_response(422, {"error": "Bài đăng này là video, hãy chọn loại Video."})
        with LOCK:
            in_progress = sum(1 for j in JOBS.values() if j["status"] in ("queued", "processing"))
            if in_progress >= MAX_JOBS:
                return self.json_response(503, {"error": "Máy chủ đang bận. Hãy thử lại trong ít phút."})
            jid = secrets.token_urlsafe(24)
            JOBS[jid] = {"created": time.time(), "status": "queued", "error": "", "bytes": 0, "mode": "file"}
        POOL.submit(download_media, jid, url, file_type)
        return self.json_response(202, {"job_id": jid, "status": "queued"})


def main():
    port = int(os.environ.get("PORT", "8000"))
    STORAGE.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), App)
    print(f"HimTik running on http://0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        POOL.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
