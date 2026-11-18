import http.client
import http.cookiejar
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
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

    def test_facebook_instagram(self):
        self.assertEqual(server.extract_url('https://www.facebook.com/reel/1234567890123456'), 'https://www.facebook.com/reel/1234567890123456')
        self.assertEqual(server.extract_url('https://fb.watch/abc123/'), 'https://fb.watch/abc123/')
        self.assertEqual(server.extract_url('https://www.instagram.com/reel/AbCdEfGhIjK/'), 'https://www.instagram.com/reel/AbCdEfGhIjK/')

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

    @patch('server.subprocess.run')
    def test_inspect_facebook_source(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({'title':'Ví dụ', 'uploader':'abc', 'duration':29, 'formats':[{'format_id':'x'}], 'extractor_key':'Facebook'})
        data = server.inspect_media('https://www.facebook.com/reel/123')
        self.assertEqual(data['source'], 'Facebook')

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
        self.assertIn('raw', server.META_CACHE[url])

    @patch('server.subprocess.Popen')
    def test_pipe_uses_load_info_json(self, popen):
        raw = {"id": "123", "formats": fake_info()["formats"]}
        stream, close_fn = server.open_ytdlp_pipe('https://www.tiktok.com/@abc/video/123', raw)
        cmd = popen.call_args[0][0]
        self.assertIn('--load-info-json', cmd)
        info_path = cmd[cmd.index('--load-info-json') + 1]
        with open(info_path) as handle:
            self.assertEqual(json.load(handle), raw)
        self.assertIn('https://www.tiktok.com/@abc/video/123', cmd)
        close_fn()
        self.assertFalse(os.path.exists(info_path))

    @patch('server.subprocess.Popen')
    def test_pipe_without_raw_uses_plain_url(self, popen):
        server.open_ytdlp_pipe('https://www.tiktok.com/@abc/video/123')
        cmd = popen.call_args[0][0]
        self.assertNotIn('--load-info-json', cmd)


class ImageTests(unittest.TestCase):
    def setUp(self):
        with server.LOCK:
            server.META_CACHE.clear()
            server.JOBS.clear()

    def test_ytdlp_base_default_has_no_playlist(self):
        self.assertIn("--no-playlist", server.ytdlp_base())

    def test_ytdlp_base_playlist_mode_drops_flag(self):
        self.assertNotIn("--no-playlist", server.ytdlp_base(playlist=True))
        self.assertIn("--no-warnings", server.ytdlp_base(playlist=True))

    @patch('server.subprocess.run')
    def test_inspect_playlist_allowed_in_image_mode(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({
            '_type': 'playlist', 'title': 'Album', 'uploader': 'abc',
            'entries': [{'id': '1'}, {'id': '2'}, {'id': '3'}],
            'extractor_key': 'TikTok'})
        url = 'https://www.tiktok.com/@abc/photo/123'
        data = server.inspect_media(url, allow_playlist=True)
        self.assertEqual(data['images'], 3)
        self.assertNotIn(url, server.META_CACHE)
        # The same playlist is still rejected in video mode (no cache leak).
        with self.assertRaises(ValueError):
            server.inspect_media(url)
        self.assertEqual(run.call_count, 2)
        # Image mode dropped --no-playlist; video mode kept it.
        self.assertNotIn("--no-playlist", run.call_args_list[0][0][0])
        self.assertIn("--no-playlist", run.call_args_list[1][0][0])

    @patch('server.subprocess.run')
    def test_inspect_single_image_has_images_1_and_formats(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({
            'title': 'Anh', 'uploader': 'abc', 'duration': None,
            'formats': [{'format_id': 'img', 'ext': 'jpg', 'vcodec': 'none'}],
            'extractor_key': 'Instagram'})
        data = server.inspect_media('https://www.instagram.com/p/AbCd/', allow_playlist=True)
        self.assertEqual(data['images'], 1)
        self.assertEqual(data['formats'][0]['vcodec'], 'none')
        self.assertEqual(data['source'], 'Instagram')

    def test_public_info_images_only_when_present(self):
        self.assertNotIn('images', server.public_info({'title': 'V', 'source': 'TikTok'}))
        self.assertEqual(server.public_info({'title': 'A', 'source': 'TikTok', 'images': 3})['images'], 3)

    def test_is_image_bytes(self):
        self.assertTrue(server.is_image_bytes(b'\xff\xd8\xff' + b'x' * 9, '.jpg'))
        self.assertTrue(server.is_image_bytes(b'\x89PNG\r\n\x1a\n' + b'x' * 4, '.png'))
        self.assertTrue(server.is_image_bytes(b'RIFFxxxxWEBP', '.webp'))
        self.assertTrue(server.is_image_bytes(b'GIF89a' + b'x' * 6, '.gif'))
        self.assertTrue(server.is_image_bytes(b'BM' + b'x' * 10, '.bmp'))
        self.assertTrue(server.is_image_bytes(b'\x00\x00\x00\x18ftypavif', '.avif'))
        self.assertFalse(server.is_image_bytes(b'not an image at all', '.jpg'))
        self.assertFalse(server.is_image_bytes(b'\xff\xd8\xff' + b'x' * 9, '.png'))

    def test_image_sort_key_na_counts_as_zero(self):
        self.assertEqual(server._image_sort_key(Path('NA_abc.jpg')), (0, 'NA_abc.jpg'))
        self.assertEqual(server._image_sort_key(Path('02_abc.jpg')), (2, '02_abc.jpg'))
        self.assertEqual(server._image_sort_key(Path('01_abc.jpg')), (1, '01_abc.jpg'))

    @patch('server.subprocess.run')
    def test_download_media_video_command_unchanged(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                jid = 'cmdjob'
                url = 'https://www.tiktok.com/@abc/video/123'
                with server.LOCK:
                    server.JOBS[jid] = {'created': time.time(), 'status': 'queued'}
                def simulate(command, **kwargs):
                    Path(tmp, jid, 'test.mp4').write_bytes(b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00video')
                    return type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
                run.side_effect = simulate
                server.download_media(jid, url, 'video')
                bin_name = os.environ.get("YTDLP_BIN", "yt-dlp")
                expected = [bin_name, "--no-playlist", "--no-warnings", "--no-progress",
                            "--socket-timeout", "12", "--retries", "1", "--fragment-retries", "1",
                            "--restrict-filenames", "--max-filesize", "120M",
                            "--output", str(Path(tmp, jid) / "%(id).80s.%(ext)s"),
                            "-f", "best[ext=mp4]/best", "--merge-output-format", "mp4", url]
                self.assertEqual(run.call_args[0][0], expected)
            finally:
                server.STORAGE = old_storage

    @patch('server.subprocess.run')
    def test_download_media_audio_command_unchanged(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                jid = 'cmdjob'
                url = 'https://www.tiktok.com/@abc/video/123'
                with server.LOCK:
                    server.JOBS[jid] = {'created': time.time(), 'status': 'queued'}
                def simulate(command, **kwargs):
                    Path(tmp, jid, 'test.mp3').write_bytes(b'ID3' + b'\x00' * 20)
                    return type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
                run.side_effect = simulate
                server.download_media(jid, url, 'audio')
                bin_name = os.environ.get("YTDLP_BIN", "yt-dlp")
                expected = [bin_name, "--no-playlist", "--no-warnings", "--no-progress",
                            "--socket-timeout", "12", "--retries", "1", "--fragment-retries", "1",
                            "--restrict-filenames", "--max-filesize", "120M",
                            "--output", str(Path(tmp, jid) / "%(id).80s.%(ext)s"),
                            "-f", "bestaudio/best", "-x", "--audio-format", "mp3",
                            "--audio-quality", "192K", url]
                self.assertEqual(run.call_args[0][0], expected)
            finally:
                server.STORAGE = old_storage

    @patch('server.subprocess.run')
    def test_worker_creates_image_files(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                jid = 'imgjob'
                with server.LOCK:
                    server.JOBS[jid] = {'created': time.time(), 'status': 'queued'}
                def simulate(command, **kwargs):
                    folder = Path(tmp, jid)
                    (folder / '02_aaa.jpg').write_bytes(b'\xff\xd8\xff' + b'jpg2')
                    (folder / '01_bbb.jpg').write_bytes(b'\xff\xd8\xff' + b'jpg1')
                    return type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
                run.side_effect = simulate
                server.download_media(jid, 'https://www.tiktok.com/@abc/photo/123', 'image')
                with server.LOCK:
                    self.assertEqual(server.JOBS[jid]['status'], 'ready')
                    files = server.JOBS[jid]['image_files']
                self.assertEqual(len(files), 2)
                self.assertEqual([Path(p).name for p in files], ['01.jpg', '02.jpg'])
                self.assertFalse(list(Path(tmp, jid).glob('*.zip')))
                # The yt-dlp command for images drops --no-playlist.
                self.assertNotIn("--no-playlist", run.call_args[0][0])
            finally:
                server.STORAGE = old_storage

    @patch('server.subprocess.run')
    def test_single_image_na_prefix_sorts_fine(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                jid = 'imgjob1'
                with server.LOCK:
                    server.JOBS[jid] = {'created': time.time(), 'status': 'queued'}
                def simulate(command, **kwargs):
                    Path(tmp, jid, 'NA_ccc.jpg').write_bytes(b'\xff\xd8\xff' + b'jpg')
                    return type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
                run.side_effect = simulate
                server.download_media(jid, 'https://www.tiktok.com/@abc/photo/123', 'image')
                with server.LOCK:
                    self.assertEqual(server.JOBS[jid]['status'], 'ready')
                    self.assertEqual([Path(p).name for p in server.JOBS[jid]['image_files']], ['01.jpg'])
            finally:
                server.STORAGE = old_storage

    @patch('server.subprocess.run')
    def test_image_branch_rejects_video_file(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                jid = 'imgjob2'
                with server.LOCK:
                    server.JOBS[jid] = {'created': time.time(), 'status': 'queued'}
                def simulate(command, **kwargs):
                    Path(tmp, jid, 'clip.mp4').write_bytes(b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00video')
                    return type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
                run.side_effect = simulate
                server.download_media(jid, 'https://www.tiktok.com/@abc/video/123', 'image')
                with server.LOCK:
                    self.assertEqual(server.JOBS[jid]['status'], 'error')
                    self.assertIn('video', server.JOBS[jid]['error'])
            finally:
                server.STORAGE = old_storage

    @patch('server.subprocess.run')
    def test_image_branch_total_over_limit(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            old_max = server.MAX_BYTES
            server.MAX_BYTES = 10
            try:
                jid = 'imgjob3'
                with server.LOCK:
                    server.JOBS[jid] = {'created': time.time(), 'status': 'queued'}
                def simulate(command, **kwargs):
                    Path(tmp, jid, '01_a.jpg').write_bytes(b'\xff\xd8\xff' + b'x' * 20)
                    Path(tmp, jid, '02_b.jpg').write_bytes(b'\xff\xd8\xff' + b'y' * 20)
                    return type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
                run.side_effect = simulate
                server.download_media(jid, 'https://www.tiktok.com/@abc/photo/123', 'image')
                with server.LOCK:
                    self.assertEqual(server.JOBS[jid]['status'], 'error')
                    self.assertIn('120 MB', server.JOBS[jid]['error'])
            finally:
                server.STORAGE = old_storage
                server.MAX_BYTES = old_max


class GalleryTests(unittest.TestCase):
    """Instagram/Facebook photo posts go through gallery-dl (2.0.5)."""

    def setUp(self):
        with server.LOCK:
            server.META_CACHE.clear()
            server.JOBS.clear()

    def _gallery_simulate(self, tmp, jid, probe_stdout, probe_rc=0, probe_stderr='',
                          download_files=(), download_rc=0, download_stderr=''):
        """Mock subprocess.run: probe (--get-urls) prints URLs, download writes files."""
        calls = []
        def simulate(command, **kwargs):
            calls.append(list(command))
            if '--get-urls' in command:
                return type('Result', (), {'returncode': probe_rc, 'stdout': probe_stdout,
                                           'stderr': probe_stderr})()
            folder = Path(tmp, jid)
            for rel, data in download_files:
                p = folder / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            return type('Result', (), {'returncode': download_rc, 'stdout': '',
                                       'stderr': download_stderr})()
        return simulate, calls

    def _run_job(self, tmp, jid, url, simulate):
        with server.LOCK:
            server.JOBS[jid] = {'created': time.time(), 'status': 'queued'}
        with patch('server.subprocess.run') as run:
            run.side_effect = simulate
            server.download_media(jid, url, 'image')
        with server.LOCK:
            return dict(server.JOBS[jid])

    def test_gallery_platform_detection(self):
        self.assertEqual(server._gallery_platform('https://www.instagram.com/p/AbCd/'), 'instagram')
        self.assertEqual(server._gallery_platform('https://www.facebook.com/photo.php?fbid=1'), 'facebook')
        self.assertIsNone(server._gallery_platform('https://www.tiktok.com/@a/photo/1'))
        self.assertIsNone(server._gallery_platform('https://fb.watch/abc'))

    @patch('server.subprocess.run')
    def test_gallery_single_image_served_as_file(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                jpg = b'\xff\xd8\xff' + b'jpg1'
                simulate, calls = self._gallery_simulate(
                    tmp, 'gal1', 'https://cdn.example.com/1.jpg\n',
                    download_files=[('instagram/user/abc_1.jpg', jpg)])
                run.side_effect = simulate
                job = self._run_job(tmp, 'gal1', 'https://www.instagram.com/p/AbCd/', simulate)
                self.assertEqual(job['status'], 'ready')
                self.assertEqual(job['found'], 1)
                self.assertEqual(job['downloaded'], 1)
                self.assertNotIn('warning', job)
                self.assertNotIn('filename', job)
                self.assertEqual([Path(p).name for p in job['image_files']], ['01.jpg'])
                # No cookies configured: no --cookies flag anywhere.
                self.assertTrue(all('--cookies' not in c for c in calls))
            finally:
                server.STORAGE = old_storage

    @patch('server.subprocess.run')
    def test_gallery_multiple_images_served_as_files(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                jpgs = [b'\xff\xd8\xff' + bytes([i]) * 4 for i in range(3)]
                probe = '\n'.join(f'https://cdn.example.com/{i}.jpg' for i in range(3)) + '\n'
                simulate, _ = self._gallery_simulate(
                    tmp, 'gal3', probe,
                    download_files=[(f'instagram/user/{i}.jpg', jpgs[i]) for i in range(3)])
                run.side_effect = simulate
                job = self._run_job(tmp, 'gal3', 'https://www.instagram.com/p/AbCd/', simulate)
                self.assertEqual(job['status'], 'ready')
                self.assertEqual(job['found'], 3)
                self.assertEqual(job['downloaded'], 3)
                self.assertNotIn('warning', job)
                self.assertNotIn('filename', job)  # 2.0.6: no zip, individual files
                self.assertEqual([Path(p).name for p in job['image_files']],
                                 ['01.jpg', '02.jpg', '03.jpg'])
                self.assertEqual(job['bytes'], sum(len(j) for j in jpgs))
                self.assertEqual(Path(job['image_files'][1]).read_bytes(), jpgs[1])
            finally:
                server.STORAGE = old_storage

    @patch('server.subprocess.run')
    def test_gallery_login_required_no_cookie(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                simulate, calls = self._gallery_simulate(
                    tmp, 'galfb', '', probe_rc=16,
                    probe_stderr='[facebook][error] AuthRequired: You must be logged in to continue viewing images.')
                run.side_effect = simulate
                job = self._run_job(tmp, 'galfb', 'https://www.facebook.com/photo.php?fbid=1', simulate)
                self.assertEqual(job['status'], 'error')
                self.assertEqual(job['error'], 'Bài này cần đăng nhập, chưa có cookie.')
                self.assertEqual(len(calls), 1)  # probe only, no download attempt
                self.assertTrue(all('--cookies' not in c for c in calls))
            finally:
                server.STORAGE = old_storage

    @patch('server.subprocess.run')
    def test_gallery_login_required_with_cookie(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                cookie_text = ('# Netscape HTTP Cookie File\n'
                               '.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tfake-test-value')
                simulate, calls = self._gallery_simulate(
                    tmp, 'galig', '', probe_rc=4,
                    probe_stderr='[instagram][error] HTTP redirect to login page (https://www.instagram.com/accounts/login/)')
                run.side_effect = simulate
                with patch.dict(os.environ, {'IG_COOKIES': cookie_text}):
                    job = self._run_job(tmp, 'galig', 'https://www.instagram.com/p/AbCd/', simulate)
                self.assertEqual(job['status'], 'error')
                self.assertEqual(job['error'], 'Bài này cần đăng nhập, cookie có thể đã hết hạn.')
                self.assertIn('--cookies', calls[0])
                cookie_path = calls[0][calls[0].index('--cookies') + 1]
                self.assertFalse(Path(cookie_path).exists())  # temp cookie file deleted
            finally:
                server.STORAGE = old_storage

    @patch('server.subprocess.run')
    def test_gallery_partial_download_warns(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                probe = '\n'.join(f'https://cdn.example.com/{i}.jpg' for i in range(8)) + '\n'
                simulate, _ = self._gallery_simulate(
                    tmp, 'gal8', probe,
                    download_files=[(f'instagram/user/{i}.jpg', b'\xff\xd8\xff' + bytes([i])) for i in range(3)])
                run.side_effect = simulate
                job = self._run_job(tmp, 'gal8', 'https://www.instagram.com/p/AbCd/', simulate)
                self.assertEqual(job['status'], 'ready')
                self.assertEqual(job['found'], 8)
                self.assertEqual(job['downloaded'], 3)
                self.assertEqual(job['warning'], 'Chỉ tải được 3/8 ảnh.')
                self.assertNotIn('filename', job)  # 2.0.6: no zip, individual files
                self.assertEqual([Path(p).name for p in job['image_files']],
                                 ['01.jpg', '02.jpg', '03.jpg'])
            finally:
                server.STORAGE = old_storage

    @patch('server.subprocess.run')
    def test_gallery_video_post_rejected(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                simulate, _ = self._gallery_simulate(
                    tmp, 'galvid', 'https://cdn.example.com/clip.mp4\n',
                    download_files=[('instagram/user/clip.mp4',
                                     b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00video')])
                run.side_effect = simulate
                job = self._run_job(tmp, 'galvid', 'https://www.instagram.com/p/AbCd/', simulate)
                self.assertEqual(job['status'], 'error')
                self.assertIn('video', job['error'])
            finally:
                server.STORAGE = old_storage

    @patch('server.subprocess.run')
    def test_gallery_over_30_rejected(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                probe = '\n'.join(f'https://cdn.example.com/{i}.jpg' for i in range(31)) + '\n'
                simulate, calls = self._gallery_simulate(tmp, 'gal31', probe)
                run.side_effect = simulate
                job = self._run_job(tmp, 'gal31', 'https://www.instagram.com/p/AbCd/', simulate)
                self.assertEqual(job['status'], 'error')
                self.assertIn('30', job['error'])
                self.assertEqual(len(calls), 1)  # probe only, no download attempt
            finally:
                server.STORAGE = old_storage


class _FakeHTTPResponse:
    """Minimal urllib response for the Facebook parser tests."""

    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n=-1):
        return self._data if n < 0 else self._data[:n]


class _FakeUrlopen:
    """Fake urllib.request.urlopen: serves canned byte responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append(request)
        return _FakeHTTPResponse(self._responses.pop(0))


class FacebookParserTests(unittest.TestCase):
    """Facebook og:image parser fallback (2.0.6)."""

    FB_COOKIE_TEXT = ('# Netscape HTTP Cookie File\n'
                      '.facebook.com\tTRUE\t/\tTRUE\t0\tc_user\t123')

    def setUp(self):
        with server.LOCK:
            server.META_CACHE.clear()
            server.JOBS.clear()

    def _cookie_file(self, tmp):
        path = Path(tmp, 'fb_cookies.txt')
        path.write_text(self.FB_COOKIE_TEXT)
        return str(path)

    def _page(self, image_url=None, video=False):
        parts = ['<html><head>']
        if video:
            parts.append('<meta property="og:video" content="https://example.com/v.mp4">')
        if image_url:
            parts.append('<meta property="og:image" content="%s">' % image_url)
        parts.append('</head><body>photo</body></html>')
        return ''.join(parts).encode('utf-8')

    def test_og_image_url_extraction(self):
        html1 = ('<meta property="og:image" content="https://scontent.example.com/a.jpg'
                 '?x=1&amp;y=2">')
        self.assertEqual(server._facebook_og_image_url(html1),
                         'https://scontent.example.com/a.jpg?x=1&y=2')
        html2 = ('<meta content="https://scontent.example.com/b.png" '
                 'property="og:image">')
        self.assertEqual(server._facebook_og_image_url(html2),
                         'https://scontent.example.com/b.png')
        self.assertIsNone(server._facebook_og_image_url('<html><head></head></html>'))
        # og:image:secure_url is the fallback when og:image itself is absent.
        self.assertEqual(server._facebook_og_image_url(
            '<meta property="og:image:secure_url" content="https://example.com/c.jpg">'),
            'https://example.com/c.jpg')

    def test_netscape_cookie_header_tolerates_cookie_editor_export(self):
        # Regression test for the live 2.0.7 failure: Cookie-Editor writes
        # leading-dot domains (".facebook.com") with the domain flag FALSE,
        # and CPython's MozillaCookieJar refuses the whole file over that
        # (LoadError: assert domain_specified == initial_dot). The parser
        # therefore reads the file by hand. Bad lines are skipped, cookies
        # for other hosts and out-of-scope paths are left out.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'fb.txt')
            path.write_text(
                '# Netscape HTTP Cookie File\n'
                '#HttpOnly_.facebook.com\tFALSE\t/\tTRUE\t0\tc_user\t123\n'
                '#HttpOnly_.facebook.com\tFALSE\t/login/device-based/\tTRUE\t0\tdbln\tx\n'
                '.facebook.com\tFALSE\t/\tTRUE\t0\tsb\tabc\n'
                '.instagram.com\tFALSE\t/\tTRUE\t0\tsessionid\tnope\n'
                'garbage line without tabs\n')
            header = server._netscape_cookie_header(
                str(path), 'https://www.facebook.com/photo.php?fbid=1')
            self.assertEqual(header, 'c_user=123; sb=abc')
            # The same file must ALSO be loadable the old way? No: it is
            # exactly the shape MozillaCookieJar rejects, which is the point.
            with self.assertRaises(Exception):
                jar = http.cookiejar.MozillaCookieJar(str(path))
                jar.load(ignore_discard=True, ignore_expires=True)

    def _parser_job(self, tmp, jid, url, opener, cookie_file):
        with server.LOCK:
            server.JOBS[jid] = {'created': time.time(), 'status': 'queued'}
        folder = Path(tmp, jid)
        folder.mkdir(parents=True, exist_ok=True)
        with patch('server.urllib.request.urlopen', opener):
            server._download_images_facebook_parser(jid, url, folder, cookie_file)
        with server.LOCK:
            return dict(server.JOBS[jid])

    def test_parser_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = b'\xff\xd8\xff' + b'fbimg'
            page = self._page('https://scontent.example.com/photo.jpg?stp=abc&amp;oh=123')
            opener = _FakeUrlopen([page, img])
            job = self._parser_job(tmp, 'fbp1', 'https://www.facebook.com/photo.php?fbid=1',
                                   opener, self._cookie_file(tmp))
            self.assertEqual(job['status'], 'ready')
            self.assertEqual(job['found'], 1)
            self.assertEqual(job['downloaded'], 1)
            self.assertEqual(job['bytes'], len(img))
            self.assertEqual([Path(p).name for p in job['image_files']], ['01.jpg'])
            self.assertEqual(Path(job['image_files'][0]).read_bytes(), img)
            self.assertEqual(len(opener.calls), 2)
            self.assertEqual(opener.calls[0].full_url,
                             'https://www.facebook.com/photo.php?fbid=1')
            self.assertEqual(opener.calls[1].full_url,
                             'https://scontent.example.com/photo.jpg?stp=abc&oh=123')

    def test_parser_no_og_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeUrlopen([self._page()])
            with self.assertRaises(ValueError) as cm:
                self._parser_job(tmp, 'fbp2', 'https://www.facebook.com/photo.php?fbid=1',
                                 opener, self._cookie_file(tmp))
            self.assertIn('Không tìm thấy ảnh', str(cm.exception))

    def test_parser_video_post_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeUrlopen([self._page('https://scontent.example.com/t.jpg', video=True)])
            with self.assertRaises(ValueError) as cm:
                self._parser_job(tmp, 'fbp3', 'https://www.facebook.com/photo.php?fbid=1',
                                 opener, self._cookie_file(tmp))
            self.assertIn('video', str(cm.exception))

    def _fallback_job(self, tmp, jid, url, simulate, opener, env):
        with server.LOCK:
            server.JOBS[jid] = {'created': time.time(), 'status': 'queued'}
        old_storage = server.STORAGE
        server.STORAGE = Path(tmp)
        try:
            with patch('server.subprocess.run') as run, \
                 patch('server.urllib.request.urlopen', opener), \
                 patch.dict(os.environ, env):
                run.side_effect = simulate
                server.download_media(jid, url, 'image')
        finally:
            server.STORAGE = old_storage
        with server.LOCK:
            return dict(server.JOBS[jid])

    def _gallery_fail_simulate(self, probe_rc=1, probe_stderr=''):
        def simulate(command, **kwargs):
            if '--get-urls' in command:
                return type('Result', (), {'returncode': probe_rc, 'stdout': '',
                                           'stderr': probe_stderr})()
            return type('Result', (), {'returncode': 1, 'stdout': '', 'stderr': ''})()
        return simulate

    def test_gallery_facebook_falls_back_to_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = b'\xff\xd8\xff' + b'fbimg'
            page = self._page('https://scontent.example.com/photo.jpg')
            opener = _FakeUrlopen([page, img])
            simulate = self._gallery_fail_simulate(
                probe_stderr="[facebook][error] An unexpected error occurred: KeyError - 'fbid'.")
            job = self._fallback_job(tmp, 'fbf1', 'https://www.facebook.com/photo.php?fbid=1',
                                     simulate, opener,
                                     {'FB_COOKIES': self.FB_COOKIE_TEXT})
            self.assertEqual(job['status'], 'ready')
            self.assertEqual(job['found'], 1)
            self.assertEqual(job['downloaded'], 1)
            self.assertEqual([Path(p).name for p in job['image_files']], ['01.jpg'])
            self.assertEqual(len(opener.calls), 2)

    def test_gallery_facebook_no_fallback_on_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeUrlopen([b'<html></html>'])
            simulate = self._gallery_fail_simulate(
                probe_rc=16,
                probe_stderr='[facebook][error] AuthRequired: You must be logged in to continue viewing images.')
            job = self._fallback_job(tmp, 'fbf2', 'https://www.facebook.com/photo.php?fbid=1',
                                     simulate, opener,
                                     {'FB_COOKIES': self.FB_COOKIE_TEXT})
            self.assertEqual(job['status'], 'error')
            self.assertEqual(job['error'], 'Bài này cần đăng nhập, cookie có thể đã hết hạn.')
            self.assertEqual(opener.calls, [])

    def test_gallery_facebook_no_fallback_on_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeUrlopen([b'<html></html>'])

            def simulate(command, **kwargs):
                if '--get-urls' in command:
                    return type('Result', (), {'returncode': 0,
                                               'stdout': 'https://cdn.example.com/clip.mp4\n',
                                               'stderr': ''})()
                folder = Path(tmp, 'fbf3')
                (folder / 'facebook' / 'user').mkdir(parents=True, exist_ok=True)
                (folder / 'facebook' / 'user' / 'clip.mp4').write_bytes(
                    b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00video')
                return type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()

            job = self._fallback_job(tmp, 'fbf3', 'https://www.facebook.com/photo.php?fbid=1',
                                     simulate, opener,
                                     {'FB_COOKIES': self.FB_COOKIE_TEXT})
            self.assertEqual(job['status'], 'error')
            self.assertIn('video', job['error'])
            self.assertEqual(opener.calls, [])

    def test_parser_error_propagates_not_gallery_error(self):
        # 2.0.7: when the parser runs and fails, the job carries the
        # parser's own (more specific) error, not the gallery-dl one.
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeUrlopen([self._page()])
            simulate = self._gallery_fail_simulate(
                probe_stderr="[facebook][error] An unexpected error occurred: KeyError - 'fbid'.")
            job = self._fallback_job(tmp, 'fbf5', 'https://www.facebook.com/photo.php?fbid=1',
                                     simulate, opener,
                                     {'FB_COOKIES': self.FB_COOKIE_TEXT})
            self.assertEqual(job['status'], 'error')
            self.assertEqual(job['error'], 'Không tìm thấy ảnh nào trong bài đăng này.')
            self.assertEqual(len(opener.calls), 1)

    def test_parser_login_page_reports_login_error(self):
        # A page with no og:image but login markers means the session was
        # refused (e.g. expired cookies), not a missing photo.
        with tempfile.TemporaryDirectory() as tmp:
            page = b'<html><head><title>Log in</title></head><body>you must be logged in</body></html>'
            opener = _FakeUrlopen([page])
            simulate = self._gallery_fail_simulate(
                probe_stderr="[facebook][error] An unexpected error occurred: KeyError - 'fbid'.")
            job = self._fallback_job(tmp, 'fbf6', 'https://www.facebook.com/photo.php?fbid=1',
                                     simulate, opener,
                                     {'FB_COOKIES': self.FB_COOKIE_TEXT})
            self.assertEqual(job['status'], 'error')
            self.assertEqual(job['error'], 'Bài này cần đăng nhập, cookie có thể đã hết hạn.')

    def test_gallery_instagram_no_parser_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener = _FakeUrlopen([b'<html></html>'])
            simulate = self._gallery_fail_simulate(
                probe_stderr='[instagram][error] HTTP 429 Too Many Requests')
            job = self._fallback_job(tmp, 'fbf4', 'https://www.instagram.com/p/AbCd/',
                                     simulate, opener, {})
            self.assertEqual(job['status'], 'error')
            self.assertEqual(job['error'], 'Không đọc được bài đăng. Hãy thử liên kết công khai khác.')
            self.assertEqual(opener.calls, [])


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

    def test_rate_limited_post_does_not_break_next_request(self):
        # A rate-limited POST must not corrupt later requests. V2 speaks
        # HTTP/1.0 (default), so every response closes its connection and
        # unread body bytes can never leak into a following request.
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            body = json.dumps({'url': 'https://www.tiktok.com/@demo/video/123',
                               'type': 'video'}).encode('utf-8')
            with patch('server.check_rate', return_value=False):
                conn.request('POST', '/api/download', body=body,
                             headers={'Content-Type': 'application/json'})
                response = conn.getresponse()
                self.assertEqual(response.status, 429)
                self.assertEqual(response.getheader('Connection'), 'close')
                response.read()
        finally:
            conn.close()
        # Fresh connection after the rejected POST must work normally.
        status, body, _ = self.request('/api/health')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['ok'])

    def test_early_post_rejection_closes_connection(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request('POST', '/wrong-route', body=b'{"url":"junk"}',
                         headers={'Content-Type': 'application/json'})
            response = conn.getresponse()
            self.assertEqual(response.status, 404)
            self.assertEqual(response.getheader('Connection'), 'close')
            response.read()
        finally:
            conn.close()

    def test_oversized_post_closes_connection(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request('POST', '/api/download', body=b'x' * (server.MAX_REQUEST + 1),
                         headers={'Content-Type': 'application/json'})
            response = conn.getresponse()
            self.assertEqual(response.status, 413)
            self.assertEqual(response.getheader('Connection'), 'close')
            response.read()
        finally:
            conn.close()

    def test_chunked_request_rejected_and_closed(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request('POST', '/api/download', body=[b'{"url":"bad"}'],
                         headers={'Content-Type': 'application/json'},
                         encode_chunked=True)
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(response.getheader('Connection'), 'close')
            response.read()
        finally:
            conn.close()

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

    def _stream_job(self, age_seconds=0):
        jid = 'streamjob123'
        with server.LOCK:
            server.JOBS[jid] = {'created': time.time() - age_seconds, 'status': 'ready', 'error': '',
                                'bytes': 0, 'mode': 'stream',
                                'direct': {'url': 'https://v16-webapp-prime.us.tiktok.com/h264.mp4',
                                           'format_id': 'h264_540p_136145-0', 'filesize': 100,
                                           'http_headers': {'User-Agent': 'test-agent'}},
                                'source_url': 'https://www.tiktok.com/@abc/video/123'}
        return jid

    def _seed_raw_cache(self, url='https://www.tiktok.com/@abc/video/123'):
        raw = {"id": "123", "formats": fake_info()["formats"],
               "webpage_url": url, "extractor_key": "TikTok"}
        with server.LOCK:
            server.META_CACHE[url] = {"created": time.time(), "info": fake_info(), "raw": raw}
        return raw

    def _pipe_stream(self, payload):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, payload)
        os.close(write_fd)
        return os.fdopen(read_fd, 'rb')

    @patch('server.open_ytdlp_pipe')
    @patch('server.inspect_media')
    def test_stream_serves_mp4_via_pipe(self, mock_inspect, mock_pipe):
        jid = self._stream_job()
        raw = self._seed_raw_cache()
        payload = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00' + b'0123456789' * 8
        stream = self._pipe_stream(payload)
        mock_pipe.return_value = (stream, lambda: stream.close())
        status, body, headers = self.request('/api/stream/' + jid)
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertEqual(headers.get('Content-Type'), 'video/mp4')
        self.assertIsNone(headers.get('Content-Length'))  # the pipe has no declared length
        mock_inspect.assert_not_called()  # cached raw info: no second extraction
        mock_pipe.assert_called_once()
        self.assertEqual(mock_pipe.call_args[0][1], raw)

    @patch('server.open_ytdlp_pipe')
    def test_stream_rejects_non_mp4(self, mock_pipe):
        jid = self._stream_job()
        self._seed_raw_cache()
        stream = self._pipe_stream(b'<html>not a video</html>')
        mock_pipe.return_value = (stream, lambda: stream.close())
        status, body, _ = self.request('/api/stream/' + jid)
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)['fallback'], 'full')

    @patch('server.open_ytdlp_pipe')
    def test_stream_pipe_failure_returns_502(self, mock_pipe):
        jid = self._stream_job()
        self._seed_raw_cache()
        mock_pipe.side_effect = RuntimeError('yt-dlp unavailable')
        status, body, _ = self.request('/api/stream/' + jid)
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)['fallback'], 'full')

    @patch('server.open_ytdlp_pipe')
    @patch('server.inspect_media')
    def test_stream_reextracts_when_cache_empty(self, mock_inspect, mock_pipe):
        jid = self._stream_job()
        raw = {"id": "123", "formats": fake_info()["formats"]}
        def seed(url, force=False):
            with server.LOCK:
                server.META_CACHE[url] = {"created": time.time(), "info": fake_info(), "raw": raw}
            return fake_info()
        mock_inspect.side_effect = seed
        payload = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00' + b'0123456789' * 8
        stream = self._pipe_stream(payload)
        mock_pipe.return_value = (stream, lambda: stream.close())
        status, body, _ = self.request('/api/stream/' + jid)
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        mock_inspect.assert_called_once()
        self.assertEqual(mock_pipe.call_args[0][1], raw)

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

    @patch('server.inspect_media')
    def test_api_inspect_image_type(self, mock):
        mock.return_value = {'title': 'Album', 'source': 'TikTok', 'images': 3}
        status, body, _ = self.request('/api/inspect', {'url': 'https://www.tiktok.com/@abc/photo/123', 'type': 'image'})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data['images'], 3)
        mock.assert_called_once_with('https://www.tiktok.com/@abc/photo/123', allow_playlist=True)

    @patch('server.inspect_media')
    @patch('server.POOL.submit')
    def test_download_image_creates_job(self, submit, mock_inspect):
        mock_inspect.return_value = {'_type': 'playlist', 'formats': [], 'images': 2}
        status, body, _ = self.request('/api/download', {'url': 'https://www.tiktok.com/@abc/photo/123', 'type': 'image'})
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)['status'], 'queued')
        submit.assert_called_once()
        mock_inspect.assert_called_once_with('https://www.tiktok.com/@abc/photo/123', allow_playlist=True)

    @patch('server.inspect_media')
    @patch('server.POOL.submit')
    def test_download_image_precheck_rejects_video(self, submit, mock_inspect):
        mock_inspect.return_value = {'_type': 'video', 'formats': [{'vcodec': 'h264', 'acodec': 'aac'}], 'images': 1}
        status, body, _ = self.request('/api/download', {'url': 'https://www.tiktok.com/@abc/video/123', 'type': 'image'})
        self.assertEqual(status, 422)
        self.assertIn('video', json.loads(body)['error'])
        submit.assert_not_called()

    @patch('server.inspect_media')
    def test_download_image_inspect_timeout(self, mock_inspect):
        mock_inspect.side_effect = server.subprocess.TimeoutExpired('yt-dlp', 40)
        status, body, _ = self.request('/api/download', {'url': 'https://www.tiktok.com/@abc/photo/123', 'type': 'image'})
        self.assertEqual(status, 504)

    @patch('server.inspect_media')
    def test_download_image_inspect_oserror(self, mock_inspect):
        mock_inspect.side_effect = OSError('no yt-dlp')
        status, body, _ = self.request('/api/download', {'url': 'https://www.tiktok.com/@abc/photo/123', 'type': 'image'})
        self.assertEqual(status, 422)

    @patch('server.subprocess.run')
    def test_image_job_files_served_inline(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                jid = 'imgjob'
                with server.LOCK:
                    server.JOBS[jid] = {'created': time.time(), 'status': 'queued'}
                def simulate(command, **kwargs):
                    folder = Path(tmp, jid)
                    (folder / '02_aaa.jpg').write_bytes(b'\xff\xd8\xff' + b'jpg2')
                    (folder / '01_bbb.jpg').write_bytes(b'\xff\xd8\xff' + b'jpg1')
                    return type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
                run.side_effect = simulate
                server.download_media(jid, 'https://www.tiktok.com/@abc/photo/123', 'image')
                status, body, _ = self.request('/api/jobs/' + jid)
                data = json.loads(body)
                self.assertEqual(status, 200)
                self.assertEqual([f['name'] for f in data['files']], ['01.jpg', '02.jpg'])
                self.assertEqual(data['files'][0]['url'], '/api/files/' + jid + '/0')
                self.assertNotIn('download_url', data)
                status, body, headers = self.request('/api/files/' + jid + '/0')
                self.assertEqual(status, 200)
                self.assertEqual(body, b'\xff\xd8\xff' + b'jpg1')
                self.assertEqual(headers.get('Content-Type'), 'image/jpeg')
                self.assertIn('inline', headers.get('Content-Disposition'))
                status, _, _ = self.request('/api/files/' + jid + '/99')
                self.assertEqual(status, 404)
                status, _, _ = self.request('/api/files/' + jid + '/abc')
                self.assertEqual(status, 404)
            finally:
                server.STORAGE = old_storage

    @patch('server.inspect_media')
    @patch('server.POOL.submit')
    def test_download_ig_image_skips_ytdlp_inspect(self, submit, mock_inspect):
        # Instagram/Facebook photo posts go through gallery-dl, so the
        # yt-dlp inspect pre-check is skipped for them entirely.
        status, body, _ = self.request('/api/download', {'url': 'https://www.instagram.com/p/AbCd/', 'type': 'image'})
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)['status'], 'queued')
        submit.assert_called_once()
        mock_inspect.assert_not_called()

    @patch('server.subprocess.run')
    def test_gallery_multi_image_job_payload_and_files(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            old_storage = server.STORAGE
            server.STORAGE = Path(tmp)
            try:
                jid = 'galfiles'
                with server.LOCK:
                    server.JOBS[jid] = {'created': time.time(), 'status': 'queued'}
                def simulate(command, **kwargs):
                    if '--get-urls' in command:
                        return type('Result', (), {'returncode': 0,
                                                   'stdout': 'https://cdn.example.com/0.jpg\nhttps://cdn.example.com/1.jpg\n',
                                                   'stderr': ''})()
                    folder = Path(tmp, jid)
                    (folder / 'instagram' / 'user').mkdir(parents=True, exist_ok=True)
                    (folder / 'instagram' / 'user' / '0.jpg').write_bytes(b'\xff\xd8\xff' + b'a')
                    (folder / 'instagram' / 'user' / '1.jpg').write_bytes(b'\xff\xd8\xff' + b'b')
                    return type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
                run.side_effect = simulate
                server.download_media(jid, 'https://www.instagram.com/p/AbCd/', 'image')
                status, body, _ = self.request('/api/jobs/' + jid)
                data = json.loads(body)
                self.assertEqual(status, 200)
                self.assertEqual(data['status'], 'ready')
                self.assertEqual(data['found'], 2)
                self.assertEqual(data['downloaded'], 2)
                self.assertNotIn('warning', data)
                self.assertNotIn('download_url', data)  # 2.0.6: no zip
                self.assertEqual([f['name'] for f in data['files']], ['01.jpg', '02.jpg'])
                self.assertEqual(data['files'][0]['url'], '/api/files/' + jid + '/0')
                self.assertEqual(data['files'][1]['url'], '/api/files/' + jid + '/1')
                status, body, headers = self.request('/api/files/' + jid + '/0')
                self.assertEqual(status, 200)
                self.assertEqual(body, b'\xff\xd8\xff' + b'a')
                self.assertEqual(headers.get('Content-Type'), 'image/jpeg')
                self.assertIn('inline', headers.get('Content-Disposition'))
                status, body, headers = self.request('/api/files/' + jid + '/1')
                self.assertEqual(status, 200)
                self.assertEqual(body, b'\xff\xd8\xff' + b'b')
            finally:
                server.STORAGE = old_storage

    def test_missing_file(self):
        status, body, _ = self.request('/api/files/unknown')
        self.assertEqual(status,404)


if __name__ == '__main__':
    unittest.main()
