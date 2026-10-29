"""Gateway contract tests. Engine responses are mocked; real ASR is checked separately in a browser."""
import io
import asyncio
import importlib.util
import signal
import subprocess
import sys
import json
import os
from uuid import uuid4
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

from fastapi.testclient import TestClient
import httpx
import app as web


class GatewayTests(unittest.TestCase):
    def test_upload_admission_precedes_body_parsing(self):
        consumed = []
        def body():
            consumed.append(True)
            yield b'not a multipart body'
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'DATA', Path(tmp)):
            with TestClient(web.app) as client, patch.object(web, 'slots', asyncio.Semaphore(0)):
                response = client.post('/api/jobs', content=body(), headers={'content-length': '20', 'content-type': 'multipart/form-data; boundary=test'})
                self.assertEqual(response.status_code, 429)
                self.assertEqual(consumed, [])
            with TestClient(web.app) as client:
                response = client.post('/api/jobs', content=b'x', headers={'content-length': '1', 'transfer-encoding': 'chunked'})
                self.assertEqual(response.status_code, 400)

    def test_worker_failure_disables_health_and_new_uploads(self):
        real_save = web.save_job
        def fail_running(job):
            if job['status'] in {'running', 'error'}: raise OSError('simulated full disk')
            real_save(job)
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(b'\0\0' * 1600)
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'DATA', Path(tmp)), patch.object(web, 'save_job', side_effect=fail_running), self.assertLogs('app', level='ERROR'):
            with TestClient(web.app) as client:
                self.assertEqual(client.post('/api/jobs', files={'file': ('test.wav', audio.getvalue())}).status_code, 202)
                for _ in range(100):
                    if web.app.state.worker.done(): break
                    time.sleep(.01)
                self.assertTrue(web.app.state.worker.done())
                self.assertFalse(client.get('/api/health').json()['ok'])
                self.assertEqual(client.post('/api/jobs', content=b'x').status_code, 503)

    def test_trailing_slash_engine_url_is_usable(self):
        with patch.dict(os.environ, {'TRANSUB_ENGINE_URL': 'http://engine.test:8765/'}):
            spec = importlib.util.spec_from_file_location('trailing_url_app', web.__file__)
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        calls = []
        def engine(request):
            calls.append(request.url.path)
            if request.url.path == '/v1/health': return httpx.Response(200, json={'ok': True})
            if request.url.path == '/v1/models': return httpx.Response(200, json={'models': [{'id': module.MODEL, 'installed': True}]})
            return httpx.Response(404)
        real_client = httpx.AsyncClient
        with tempfile.TemporaryDirectory() as tmp, patch.object(module, 'DATA', Path(tmp)), patch.object(module, 'desktop_runtime_ready', return_value=True), patch.object(module, 'local_translation_ready', return_value=False), patch.object(module.httpx, 'AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(engine), **kw)):
            with TestClient(module.app) as client:
                self.assertTrue(client.get('/api/health').json()['ok'])
                self.assertEqual(calls, ['/v1/health', '/v1/models'])

    @unittest.skipUnless(os.name == 'posix', 'POSIX process groups')
    def test_pipeline_cleanup_kills_descendants(self):
        async def check():
            child = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
            parent = "import subprocess,sys,time,signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);p=subprocess.Popen([sys.executable,'-c',sys.argv[1]]);print(p.pid,flush=True);time.sleep(60)"
            process = await asyncio.create_subprocess_exec(sys.executable, '-c', parent, child, stdout=asyncio.subprocess.PIPE, start_new_session=True)
            child_pid = int(await process.stdout.readline())
            try:
                await asyncio.wait_for(web.stop_pipeline_process(process), timeout=6)
                for _ in range(100):
                    stat = Path(f'/proc/{child_pid}/stat')
                    if not stat.exists() or stat.read_text().split()[2] == 'Z': return
                    await asyncio.sleep(.01)
                self.fail('pipeline descendant survived cleanup')
            finally:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
        asyncio.run(check())

    @unittest.skipUnless(os.name == 'posix', 'POSIX foreground launcher')
    def test_foreground_shutdown_reaps_engine_and_web_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'web').mkdir(); (root / '.venv/bin').mkdir(parents=True)
            import shutil
            shutil.copyfile(Path(web.__file__).with_name('run.sh'), root / 'web/run.sh')
            worker = root / '.venv/bin/python'
            worker.write_text('#!' + sys.executable + '\n' +
                "import os,subprocess,sys,time\n" +
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n" +
                "with open(os.environ['TRANSUB_TEST_PIDS'],'a') as f:f.write(str(os.getpid())+' '+str(child.pid)+'\\n')\n" +
                "time.sleep(60)\n")
            worker.chmod(0o755)
            records = root / 'pids'; env = dict(os.environ, TRANSUB_TEST_PIDS=str(records))
            launcher = subprocess.Popen(['bash', str(root / 'web/run.sh')], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pids = []
            try:
                for _ in range(100):
                    if records.exists() and len(records.read_text().splitlines()) == 2: break
                    time.sleep(.05)
                self.assertTrue(records.exists())
                pids = [int(value) for value in records.read_text().split()]
                self.assertEqual(len(pids), 4)
                launcher.terminate(); launcher.wait(timeout=15)
                for pid in pids:
                    stat = Path(f'/proc/{pid}/stat')
                    self.assertTrue(not stat.exists() or stat.read_text().split()[2] == 'Z', f'foreground descendant {pid} survived')
            finally:
                if launcher.poll() is None: launcher.kill(); launcher.wait()
                for pid in pids:
                    try: os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError: pass

    @unittest.skipUnless(os.name == 'posix', 'POSIX foreground launcher')
    def test_launcher_allows_web_cleanup_after_engine_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'web').mkdir(); (root / '.venv/bin').mkdir(parents=True)
            import shutil
            shutil.copyfile(Path(web.__file__).with_name('run.sh'), root / 'web/run.sh')
            worker = root / '.venv/bin/python'
            fixture = """import os,signal,subprocess,sys,time
if 'transub_engine.server:create_app' in sys.argv:
    time.sleep(.8)
else:
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
    def stop(*args):
        time.sleep(.2)
        os.killpg(child.pid,signal.SIGTERM)
        sys.exit(0)
    signal.signal(signal.SIGTERM,stop)
    with open(os.environ['TRANSUB_TEST_PIDS'],'w') as f:f.write(str(child.pid))
    time.sleep(60)
"""
            worker.write_text('#!' + sys.executable + '\n' + fixture); worker.chmod(0o755)
            record = root / 'pid'; env = dict(os.environ, TRANSUB_TEST_PIDS=str(record))
            launcher = subprocess.Popen(['bash', str(root / 'web/run.sh')], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            child_pid = None
            try:
                for _ in range(100):
                    if record.exists(): break
                    time.sleep(.02)
                self.assertTrue(record.exists()); child_pid = int(record.read_text())
                launcher.wait(timeout=15)
                stat = Path(f'/proc/{child_pid}/stat')
                self.assertTrue(not stat.exists() or stat.read_text().split()[2] == 'Z', 'Web cleanup was interrupted when engine exited')
            finally:
                if launcher.poll() is None: launcher.kill(); launcher.wait()
                if child_pid:
                    try: os.killpg(child_pid, signal.SIGKILL)
                    except ProcessLookupError: pass

    def test_outputs_reject_paths_outside_task(self):
        job = {'id': str(uuid4())}
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'DATA', Path(tmp)):
            output = Path(tmp) / 'unrelated.srt'
            output.write_text('private')
            with self.assertRaises(RuntimeError):
                web.pipeline_outputs(job, {'results': [{'sourceSubtitlePath': str(output)}]})

    def test_log_retention_and_duplicate_events(self):
        job = {'id': str(uuid4())}
        with patch.object(web, 'save_job'):
            for number in range(510):
                web.append_log(job, f'event {number}', stage='asr')
            web.append_log(job, 'event 509', stage='asr')
        self.assertEqual(len(job['logs']), 500)
        self.assertEqual(job['logs'][0]['message'], 'event 10')
        self.assertEqual(job['logs'][-1]['message'], 'event 509')

    def test_restart_marks_incomplete_jobs_and_expires_old_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'DATA', Path(tmp)):
            interrupted = Path(tmp) / str(uuid4())
            interrupted.mkdir()
            job = {'id': interrupted.name, 'status': 'running', 'extension': '.wav'}
            (interrupted / 'job.json').write_text(json.dumps(job))
            (interrupted / 'input.wav').write_bytes(b'incomplete')
            expired = Path(tmp) / str(uuid4())
            expired.mkdir()
            os.utime(expired, (0, 0))
            with TestClient(web.app) as client:
                state = client.get('/api/jobs/' + interrupted.name).json()
                self.assertEqual(state['status'], 'error')
                self.assertIn('重启', state['error'])
                self.assertFalse((interrupted / 'input.wav').exists())
                self.assertFalse(expired.exists())

    def test_upload_validation_status_and_download(self):
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b'\0\0' * 1600)
        async def pipeline(job):
            self.assertEqual(job['task'], 'transcribe')
            web.append_log(job, '正在识别 <script>alert(1)</script> (42%)', stage='transcribe')
            output = web.job_dir(job['id']) / 'output/input.srt'
            output.parent.mkdir()
            output.write_text('1\n00:00:00,000 --> 00:00:01,000\nTest subtitle\n')
            (output.parent / 'pipeline-report.json').write_text('{}')
            return {'ok': True, 'results': [{'ok': True, 'sourceSubtitlePath': str(output), 'qcIssueCount': 2}]}

        with tempfile.TemporaryDirectory() as tmp, patch.object(web, 'DATA', Path(tmp)), patch.object(web, 'run_desktop_pipeline', side_effect=pipeline):
            with TestClient(web.app) as client:
                self.assertEqual(client.get('/').status_code, 200)
                self.assertEqual(client.get('/api/jobs/invalid').status_code, 404)
                self.assertEqual(client.post('/api/jobs', files={'file': ('bad.exe', b'bad')}).status_code, 400)
                self.assertEqual(client.post('/api/jobs', files={'file': ('bad.mp4', b'bad')}).status_code, 400)
                self.assertEqual(client.post('/api/jobs', files={'file': ('empty.wav', b'')}).status_code, 400)
                self.assertEqual(client.post('/api/jobs', files={'file': ('a.wav', audio.getvalue())}, data={'task': 'unknown'}).status_code, 400)
                with patch.object(web, 'local_translation_ready', return_value=False):
                    self.assertEqual(client.post('/api/jobs', files={'file': ('a.wav', audio.getvalue())}, data={'task': 'dual'}).status_code, 503)
                self.assertEqual(client.post('/api/jobs', headers={'origin': 'http://elsewhere'}, files={'file': ('a.wav', audio.getvalue())}).status_code, 403)
                self.assertEqual(client.post('/api/jobs', headers={'content-length': str(web.MAX_BYTES * 2)}, content=b'x').status_code, 413)
                r = client.post('/api/jobs', files={'file': ('../视频.wav', audio.getvalue())}, data={'language': 'en'})
                self.assertEqual(r.status_code, 202, r.text)
                job_id = r.json()['id']
                for _ in range(100):
                    state = client.get('/api/jobs/' + job_id).json()
                    if state['status'] in {'done', 'error'}:
                        break
                    time.sleep(.02)
                self.assertEqual(state['status'], 'done', state)
                self.assertNotIn('subtitle', state)
                messages = [entry['message'] for entry in state['logs']]
                self.assertIn('正在识别 <script>alert(1)</script> (42%)', messages)
                self.assertEqual(state['logs'][-1]['stage'], 'done')
                self.assertEqual(state['qcIssueCount'], 2)
                self.assertEqual(state['outputs'][0]['label'], '原文字幕')
                self.assertNotIn('path', state['outputs'][0])
                self.assertEqual(client.get('/api/jobs/' + job_id + '/files/0').status_code, 200)
                self.assertEqual(client.get('/api/jobs/' + job_id + '/files/-1').status_code, 404)
                self.assertEqual(client.get('/api/jobs/' + job_id + '/files/99').status_code, 404)
                self.assertEqual(client.get('/api/jobs/' + job_id).json()['logs'], state['logs'])
                self.assertFalse((Path(tmp) / job_id / 'input.wav').exists())
                result = client.get('/api/jobs/' + job_id + '/subtitle')
                self.assertEqual(result.status_code, 200)
                self.assertIn('Test subtitle', result.text)
                self.assertIn('attachment;', result.headers['content-disposition'])


if __name__ == '__main__':
    unittest.main()
