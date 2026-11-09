import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server


class UrlTests(unittest.TestCase):
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

    @patch('server.POOL.submit')
    def test_create_job(self, submit):
        status, body, _ = self.request('/api/download', {'url':'https://vt.tiktok.com/AAA', 'type':'video'})
        self.assertEqual(status,202)
        jid = json.loads(body)['job_id']
        self.assertTrue(jid)
        status, body, _ = self.request('/api/jobs/'+jid)
        self.assertEqual(json.loads(body)['status'], 'queued')
        self.assertEqual(status,200)
        submit.assert_called_once()

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
                    Path(tmp, jid, 'test.mp4').write_bytes(b'video test bytes')
                    return type('Result', (), {'returncode':0, 'stdout':'', 'stderr':''})()
                run.side_effect = simulate
                server.download_media(jid, 'https://www.tiktok.com/@abc/video/123', 'video')
                with server.LOCK:
                    self.assertEqual(server.JOBS[jid]['status'], 'ready')
                status, body, headers = self.request('/api/files/'+jid)
                self.assertEqual(status,200)
                self.assertEqual(body,b'video test bytes')
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
