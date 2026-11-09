"""HimTik: small standard-library HTTP service that delegates media extraction to yt-dlp.

For TikTok/Douyin public posts only. Doesn't claim guaranteed no-watermark access.

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
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
STORAGE = Path(os.environ.get("HIMTIK_TEMP_DIR", tempfile.gettempdir())) / "himtik-jobs"
MAX_BYTES = 120 * 1024 * 1024
MAX_REQUEST = 4096
JOB_TTL_SECONDS = 20 * 60
MAX_JOBS = 8
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
        raise ValueError("Hãy dán liên kết bài đăng TikTok hoặc Douyin.")
    url = found.group(0).rstrip(".,;!?)]}）。")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    allowed = ("tiktok.com", "douyin.com", "iesdouyin.com")
    if (
        parts.scheme not in ("http", "https")
        or parts.username or parts.password
        or (parts.port not in (None, 80, 443))
        or not any(host == h or host.endswith("." + h) for h in allowed)
        or not parts.path
    ):
        raise ValueError("Chỉ hỗ trợ liên kết công khai từ TikTok hoặc Douyin.")
    return url


def ytdlp_base() -> list[str]:
    return [
        os.environ.get("YTDLP_BIN", "yt-dlp"),
        "--no-playlist", "--no-warnings", "--no-progress",
        "--socket-timeout", "12", "--retries", "1", "--fragment-retries", "1",
    ]


def inspect_media(url: str, force: bool = False) -> dict:
    """Run yt-dlp --dump-single-json once per link (cached 10 minutes).

    Returns the full parsed info (display fields + formats + http_headers) so
    callers can pick a direct MP4 URL without a second extraction run.
    """
    now = time.time()
    if not force:
        with LOCK:
            hit = META_CACHE.get(url)
            if hit and now - hit["created"] < META_CACHE_TTL_SECONDS:
                print("[timing] inspect cache-hit", flush=True)
                return hit["info"]
    started = time.monotonic()
    result = subprocess.run(
        ytdlp_base() + ["--dump-single-json", "--skip-download", "--no-check-formats", url],
        capture_output=True, text=True, timeout=40, check=False,
    )
    print(f"[timing] inspect {'forced' if force else 'miss'} {time.monotonic() - started:.1f}s rc={result.returncode}", flush=True)
    if result.returncode != 0:
        raise ValueError("Không đọc được bài đăng. Hãy thử video công khai khác hoặc kiểm tra liên kết.")
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ValueError("Nguồn video chưa được hỗ trợ. Vui lòng thử lại sau.") from None
    if not isinstance(info, dict) or info.get("_type") in ("playlist", "multi_video"):
        raise ValueError("Bài đăng dạng album/playlist chưa hỗ trợ trong phiên bản này.")
    if not info.get("formats") and not info.get("url"):
        raise ValueError("Không tìm thấy luồng video có thể tải cho bài đăng này.")
    parsed = {
        "title": str(info.get("title") or info.get("description") or "Video TikTok")[:150],
        "creator": str(info.get("uploader") or info.get("creator") or "TikTok")[:80],
        "thumbnail": str(info.get("thumbnail") or "")[:1500],
        "duration": info.get("duration") if isinstance(info.get("duration"), (int, float)) else None,
        "source": "Douyin" if "douyin" in (info.get("extractor_key") or "").lower() else "TikTok",
        "formats": info.get("formats") or [],
        "http_headers": info.get("http_headers") or {},
    }
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
    return {k: info.get(k) for k in ("title", "creator", "thumbnail", "duration", "source")}


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


def download_media(job_id: str, url: str, file_type: str) -> None:
    folder = STORAGE / job_id
    try:
        with LOCK:
            JOBS[job_id]["status"] = "processing"
        folder.mkdir(parents=True, exist_ok=True)
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
    # 30 weighted hits per IP per 10 minutes. A download costs 3, so this
    # allows 10 downloads per window: 15 was too easy to hit by tapping
    # MP4/MP3 a few times while testing on a phone (2026-11-09).
    with LOCK:
        hits = RATE.setdefault(ip, [])
        hits[:] = [v for v in hits if time.time() - v < 600]
        if len(hits) + weight > 30:
            return False
        hits.extend([time.time()] * weight)
        return True


class App(BaseHTTPRequestHandler):
    server_version = "HimTik/2.0"
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
                payload = {k: job[k] for k in ("status", "error", "bytes") if k in job} if job else None
                mode = job.get("mode") if job else None
            if payload is None:
                return self.json_response(404, {"error": "Phiên tải không tồn tại hoặc đã hết hạn."})
            if payload["status"] == "ready":
                if mode == "stream":
                    payload["mode"] = "stream"
                    payload["stream_url"] = "/api/stream/" + jid
                else:
                    payload["download_url"] = "/api/files/" + jid
            return self.json_response(200, payload)
        if route.startswith("/api/stream/"):
            jid = route.rsplit("/", 1)[-1]
            return self.handle_stream(jid)
        if route.startswith("/api/files/"):
            jid = route.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(jid)
                pathname = job.get("filename") if job and job["status"] == "ready" else None
            if not pathname:
                return self.json_response(404, {"error": "Tệp chưa sẵn sàng hoặc đã hết hạn."})
            path = Path(pathname)
            if not path.is_file() or path.parent != STORAGE / jid:
                return self.json_response(404, {"error": "Tệp tải không còn tồn tại."})
            file_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            name = "HimTik-" + jid[:8] + path.suffix.lower()
            self.send_response(200)
            self.send_header("Content-Type", file_type)
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
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
            return self.json_response(404, {"error": "Đường dẫn không hợp lệ."})
        ip = self.client_address[0]  # Don't trust X-Forwarded-For from arbitrary clients.
        if not check_rate(ip, 3 if route == "/api/download" else 1):
            return self.json_response(429, {"error": "Thao tác quá nhanh. Vui lòng thử lại sau ít phút."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST:
                raise ValueError("Dữ liệu yêu cầu quá lớn hoặc bị thiếu.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Dữ liệu yêu cầu không hợp lệ.")
            url = extract_url(payload.get("url", ""))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.json_response(400, {"error": str(exc) or "Dữ liệu không hợp lệ."})
        if route == "/api/inspect":
            try:
                return self.json_response(200, public_info(inspect_media(url)))
            except subprocess.TimeoutExpired:
                return self.json_response(504, {"error": "Nguồn phản hồi quá chậm. Vui lòng thử lại."})
            except ValueError as exc:
                return self.json_response(422, {"error": str(exc)})
            except OSError:
                return self.json_response(422, {"error": "Máy chủ chưa cài yt-dlp hoặc FFmpeg."})
        file_type = payload.get("type", "video")
        if file_type not in ("video", "audio"):
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
