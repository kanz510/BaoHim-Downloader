"""HimTik: small standard-library HTTP service that delegates media extraction to yt-dlp.

For TikTok/Douyin public posts only. Doesn't claim guaranteed no-watermark access.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
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


def inspect_media(url: str) -> dict:
    result = subprocess.run(
        ytdlp_base() + ["--dump-single-json", "--skip-download", url],
        capture_output=True, text=True, timeout=40, check=False,
    )
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
    return {
        "title": str(info.get("title") or info.get("description") or "Video TikTok")[:150],
        "creator": str(info.get("uploader") or info.get("creator") or "TikTok")[:80],
        "thumbnail": str(info.get("thumbnail") or "")[:1500],
        "duration": info.get("duration") if isinstance(info.get("duration"), (int, float)) else None,
        "source": "Douyin" if "douyin" in (info.get("extractor_key") or "").lower() else "TikTok",
    }


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
    with LOCK:
        hits = RATE.setdefault(ip, [])
        hits[:] = [v for v in hits if time.time() - v < 600]
        if len(hits) + weight > 15:
            return False
        hits.extend([time.time()] * weight)
        return True


class App(BaseHTTPRequestHandler):
    server_version = "HimTik/1.0"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Don't log TikTok links or identifying query strings.
        print("[http]", self.command, self.path.split("?")[0], self.address_string(), flush=True)

    def headers_common(self):
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
            if payload is None:
                return self.json_response(404, {"error": "Phiên tải không tồn tại hoặc đã hết hạn."})
            if payload["status"] == "ready":
                payload["download_url"] = "/api/files/" + jid
            return self.json_response(200, payload)
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
                return self.json_response(200, inspect_media(url))
            except subprocess.TimeoutExpired:
                return self.json_response(504, {"error": "Nguồn phản hồi quá chậm. Vui lòng thử lại."})
            except (ValueError, OSError) as exc:
                return self.json_response(422, {"error": str(exc)})
        file_type = payload.get("type", "video")
        if file_type not in ("video", "audio"):
            return self.json_response(400, {"error": "Định dạng không hỗ trợ."})
        with LOCK:
            in_progress = sum(1 for j in JOBS.values() if j["status"] in ("queued", "processing"))
            if in_progress >= MAX_JOBS:
                return self.json_response(503, {"error": "Máy chủ đang bận. Hãy thử lại trong ít phút."})
            jid = secrets.token_urlsafe(24)
            JOBS[jid] = {"created": time.time(), "status": "queued", "error": "", "bytes": 0}
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
