import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import server


def fake_info():
    """Metadata shaped like the real TikTok extractor output."""
    return {
        "title": "Video", "creator": "abc", "thumbnail": "", "duration": 26,
        "source": "TikTok",
        "http_headers": {"User-Agent": "test-agent", "Referer": "https://www.tiktok.com/@abc/video/123"},
        "formats": [
            {"format_id": "audio", "ext": "mp3", "protocol": "https", "vcodec": "none", "acodec": "mp3",
             "url": "https://v16m.tiktokcdn-us.com/audio.mp3"},
            {"format_id": "download", "ext": "mp4", "protocol": "https", "vcodec": "h264", "acodec": "aac",
             "width": None, "height": None, "filesize": 446148,
             "url": "https://v16-webapp-prime.us.tiktok.com/download.mp4"},
            {"format_id": "h264_540p_136145-0", "ext": "mp4", "protocol": "https", "vcodec": "h264", "acodec": "aac",
             "width": 768, "height": 576, "filesize": 446148,
             "url": "https://v16-webapp-prime.us.tiktok.com/h264.mp4"},
            {"format_id": "bytevc1_720p_118748-0", "ext": "mp4", "protocol": "https", "vcodec": "h265", "acodec": "aac",
             "width": 960, "height": 720, "filesize": 389140,
             "url": "https://v16-webapp-prime.us.tiktok.com/h265.mp4"},
            {"format_id": "hls", "ext": "mp4", "protocol": "m3u8_native", "vcodec": "h264", "acodec": "aac",
             "url": "https://v16-webapp-prime.us.tiktok.com/list.m3u8"},
        ],
    }


class FakeStreamResponse:
    def __init__(self, chunks, content_length=None):
        self._data = b"".join(chunks)
        self._pos = 0
        self.headers = {"Content-Length": str(content_length)} if content_length is not None else {}

    def read(self, n=-1):
        if self._pos >= len(self._data):
            return b""
        end = len(self._data) if n < 0 else min(len(self._data), self._pos + n)
        out = self._data[self._pos:end]
        self._pos = end
        return out

    def close(self):
        pass


class UrlTests(unittest.TestCase):
    def setUp(self):
        with server.LOCK:
            server.META_CACHE.clear()
    def test_regular_tiktok(self):
        self.assertEqual(server.extract_url('https://www.tiktok.com/@abc/video/123?lang=vi'), 'https://www.tiktok.com/@abc/video/123?lang=vi')

    def test_share_text_shortlink(self):
        self.assertEqual(server.extract_url('Xem cái này https://vt.tiktok.com/ZSB12345/ nha'), 'https://vt.tiktok.com/ZSB12345/')

    def test_douyin(self):
        self.assertEqual(server.extract_url('https://v.douyin.com/abc/'), 'https://v.douyin.com/abc/')

    def test_block_unsafe_domains(self):
        for raw in ('http://localhost/private', 'https://tiktok.com.attacker.test/video', 'https://evil-tiktok.com/video', 'http://127.0.0.1/private', 'file:///etc/passwd', 'https://tiktok.com:9090/a', 'https://other.com/tiktok.com/video'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                server.extract_url(raw)

    @patch('server.subprocess.run')
    def test_inspect(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({'title':'Ví dụ', 'uploader':'abc', 'duration':29, 'formats':[{'format_id':'x'}], 'extractor_key':'TikTok'})
        data = server.inspect_media('https://www.tiktok.com/@abc/video/123')
        self.assertEqual(data['title'], 'Ví dụ')
        self.assertEqual(data['source'], 'TikTok')

    def test_pick_direct_prefers_download_then_h264(self):
        info = fake_info()
        picked = server.pick_direct(info)
        self.assertEqual(picked['format_id'], 'download')
        self.assertEqual(picked['filesize'], 446148)
        info['formats'] = [f for f in info['formats'] if f['format_id'] != 'download']
        picked = server.pick_direct(info)
        self.assertEqual(picked['format_id'], 'h264_540p_136145-0')
        info['formats'] = [f for f in info['formats'] if f['format_id'].startswith('bytevc1')]
        picked = server.pick_direct(info)
        self.assertEqual(picked['format_id'], 'bytevc1_720p_118748-0')

    def test_pick_direct_list_orders_candidates(self):
        info = fake_info()
        ordered = server.pick_direct_list(info)
        self.assertEqual([c["format_id"] for c in ordered],
                         ["download", "h264_540p_136145-0", "bytevc1_720p_118748-0"])
        # The HLS format and the audio-only format are excluded.
        self.assertTrue(all(c["url"].startswith("https://") for c in ordered))
        self.assertEqual(len({c["url"] for c in ordered}), len(ordered))
        info = fake_info()
        info['formats'] = [f for f in info['formats'] if f['format_id'] in ('audio', 'hls')]
        self.assertIsNone(server.pick_direct(info))
        self.assertIsNone(server.pick_direct({'formats': []}))
        self.assertIsNone(server.pick_direct({}))

    def test_is_mp4_prefix(self):
        self.assertTrue(server.is_mp4_prefix(b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00'))
        self.assertFalse(server.is_mp4_prefix(b'not a video file at all'))
        self.assertFalse(server.is_mp4_prefix(b'short'))

    @patch('server.socket.getaddrinfo')
    def test_validate_stream_url(self, getaddrinfo):
        getaddrinfo.return_value = [(2, 1, 6, '', ('142.250.72.14', 443))]
        self.assertTrue(server.validate_stream_url('https://v16-webapp-prime.us.tiktok.com/video.mp4'))
        self.assertFalse(server.validate_stream_url('http://v16-webapp-prime.us.tiktok.com/video.mp4'))
        self.assertFalse(server.validate_stream_url('https://user:pass@v16-webapp-prime.us.tiktok.com/video.mp4'))
        self.assertFalse(server.validate_stream_url('https://v16-webapp-prime.us.tiktok.com:8443/video.mp4'))
        getaddrinfo.return_value = [(2, 1, 6, '', ('10.0.0.5', 443))]
        self.assertFalse(server.validate_stream_url('https://v16-webapp-prime.us.tiktok.com/video.mp4'))
        getaddrinfo.return_value = [(2, 1, 6, '', ('127.0.0.1', 443))]
        self.assertFalse(server.validate_stream_url('https://v16-webapp-prime.us.tiktok.com/video.mp4'))

    @patch('server.subprocess.run')
    def test_inspect_cache(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({'title': 'Cache', 'uploader': 'abc', 'duration': 5, 'formats': [{'format_id': 'x'}], 'extractor_key': 'TikTok'})
        url = 'https://www.tiktok.com/@abc/video/999'
        server.inspect_media(url)
        server.inspect_media(url)
        self.assertEqual(run.call_count, 1)
        server.inspect_media(url, force=True)
        self.assertEqual(run.call_count, 2)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), server.App)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, url, data=None):
        req = Request(self.base+url, data=json.dumps(data).encode() if data is not None else None, headers={'Content-Type':'application/json'})
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, response.read(), response.headers
        except HTTPError as e:
            return e.code, e.read(), e.headers

    def setUp(self):
        with server.LOCK:
            server.RATE.clear()
            server.JOBS.clear()
            server.META_CACHE.clear()

    def test_health_and_site(self):
        status, body, _ = self.request('/api/health')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['ok'])
        status, body, _ = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn('HimTik'.encode(),body)
        status, body, _ = self.request('/server.py')
        self.assertEqual(status, 404)

    def test_reject_wrong_link(self):
        status, body, _ = self.request('/api/inspect', {'url':'https://example.com/file'})
        self.assertEqual(status, 400)
        self.assertIn('error',json.loads(body))

    @patch('server.inspect_media')
    def test_api_inspect(self, mock):
        mock.return_value = {'title':'Unit test video','source':'TikTok'}
        status, body, _ = self.request('/api/inspect', {'url':'https://vt.tiktok.com/AAA'})
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['title'], 'Unit test video')

    @patch('server.inspect_media')
    @patch('server.POOL.submit')
    def test_create_job(self, submit, mock_inspect):
        # No direct MP4 in the metadata: the video falls back to the file job.
        mock_inspect.return_value = {'formats': []}
        status, body, _ = self.request('/api/download', {'url':'https://vt.tiktok.com/AAA', 'type':'video'})
        self.assertEqual(status,202)
        jid = json.loads(body)['job_id']
        self.assertTrue(jid)
        status, body, _ = self.request('/api/jobs/'+jid)
        self.assertEqual(json.loads(body)['status'], 'queued')
        self.assertEqual(status,200)
        submit.assert_called_once()

    @patch('server.inspect_media')
    def test_download_returns_stream_job(self, mock_inspect):
        mock_inspect.return_value = fake_info()
        status, body, _ = self.request('/api/download', {'url':'https://www.tiktok.com/@abc/video/123', 'type':'video'})
        self.assertEqual(status, 202)
        data = json.loads(body)
        self.assertEqual(data['status'], 'ready')
        self.assertEqual(data['mode'], 'stream')
        self.assertTrue(data['stream_url'].startswith('/api/stream/'))
        jid = data['job_id']
        status, body, _ = self.request('/api/jobs/' + jid)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data['status'], 'ready')
        self.assertEqual(data['stream_url'], '/api/stream/' + jid)

    @patch('server.inspect_media')
    @patch('server.POOL.submit')
    def test_download_full_mode_skips_stream(self, submit, mock_inspect):
        mock_inspect.return_value = fake_info()
        status, body, _ = self.request('/api/download', {'url':'https://www.tiktok.com/@abc/video/123', 'type':'video', 'mode':'full'})
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)['status'], 'queued')
        submit.assert_called_once()

    def _stream_job(self):
        jid = 'streamjob123'
        with server.LOCK:
            server.JOBS[jid] = {'created': time.time(), 'status': 'ready', 'error': '',
                                'bytes': 0, 'mode': 'stream',
                                'direct': {'url': 'https://v16-webapp-prime.us.tiktok.com/h264.mp4',
                                           'format_id': 'h264_540p_136145-0', 'filesize': 100,
                                           'http_headers': {'User-Agent': 'test-agent'}},
                                'source_url': 'https://www.tiktok.com/@abc/video/123'}
        return jid

    @patch('server.open_direct')
    def test_stream_serves_mp4(self, mock_open):
        jid = self._stream_job()
        payload = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00' + b'0123456789' * 8
        mock_open.return_value = FakeStreamResponse([payload], content_length=len(payload))
        status, body, headers = self.request('/api/stream/' + jid)
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertEqual(headers.get('Content-Type'), 'video/mp4')
        self.assertIn('inline', headers.get('Content-Disposition'))
        self.assertEqual(headers.get('Content-Length'), str(len(payload)))

    @patch('server.inspect_media')
    @patch('server.open_direct')
    def test_stream_rejects_non_mp4(self, mock_open, mock_inspect):
        jid = self._stream_job()
        mock_open.return_value = FakeStreamResponse([b'<html>not a video</html>'])
        mock_inspect.return_value = {'formats': []}  # refresh finds no direct URL
        status, body, _ = self.request('/api/stream/' + jid)
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)['fallback'], 'full')

    @patch('server.inspect_media')
    @patch('server.open_direct')
    def test_stream_refresh_then_fallback(self, mock_open, mock_inspect):
        jid = self._stream_job()
        mock_open.side_effect = URLError('blocked')
        mock_inspect.return_value = {'formats': []}  # refresh finds no direct URL
        status, body, _ = self.request('/api/stream/' + jid)
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)['fallback'], 'full')
        # The single-candidate job tries its URL, then the refresh finds nothing.
        self.assertEqual(mock_open.call_count, 1)
        mock_inspect.assert_called_once()

    @patch('server.open_direct')
    def test_stream_tries_next_candidate(self, mock_open):
        jid = self._stream_job()
        with server.LOCK:
            server.JOBS[jid]["direct_list"] = [
                {"url": "https://v16-webapp-prime.us.tiktok.com/blocked.mp4",
                 "format_id": "download", "filesize": None, "http_headers": {}},
                {"url": "https://v19-webapp-prime.us.tiktok.com/h264.mp4",
                 "format_id": "h264_540p_136145-1", "filesize": 100, "http_headers": {}},
            ]
        payload = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00' + b'0123456789' * 8
        mock_open.side_effect = [URLError('blocked'),
                                 FakeStreamResponse([payload], content_length=len(payload))]
        status, body, headers = self.request('/api/stream/' + jid)
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertEqual(mock_open.call_count, 2)

    def test_stream_unknown_job(self):
        status, body, _ = self.request('/api/stream/nope')
        self.assertEqual(status, 404)

    @patch('server.subprocess.run')
    def test_worker_creates_downloadable_mp4(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                jid = 'testjob'
                with server.LOCK:
                    server.JOBS[jid] = {'created':__import__('time').time(), 'status':'queued'}
                def simulate(command, **kwargs):
                    Path(tmp, jid, 'test.mp4').write_bytes(b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00video test bytes')
                    return type('Result', (), {'returncode':0, 'stdout':'', 'stderr':''})()
                run.side_effect = simulate
                server.download_media(jid, 'https://www.tiktok.com/@abc/video/123', 'video')
                with server.LOCK:
                    self.assertEqual(server.JOBS[jid]['status'], 'ready')
                status, body, headers = self.request('/api/files/'+jid)
                self.assertEqual(status,200)
                self.assertEqual(body, b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00video test bytes')
                self.assertIn('attachment',headers.get('Content-Disposition'))
            finally:
                server.STORAGE = old_storage

    def test_non_object_payload(self):
        status, _, _ = self.request('/api/inspect', ['invalid'])
        self.assertEqual(status, 400)

    def test_reject_bad_format(self):
        status, body, _ = self.request('/api/download', {'url':'https://vt.tiktok.com/AAA', 'type':'exe'})
        self.assertEqual(status,400)

    def test_missing_file(self):
        status, body, _ = self.request('/api/files/unknown')
        self.assertEqual(status,404)


if __name__ == '__main__':
    unittest.main()
