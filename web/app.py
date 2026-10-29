"""Small LAN gateway; the engine stays on loopback and owns ASR."""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import sys
import time
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('TRANSUB_WEB_DATA', ROOT / 'data')).resolve()
ENGINE = os.environ.get('TRANSUB_ENGINE_URL', 'http://127.0.0.1:8765').rstrip('/')
MODEL = os.environ.get('TRANSUB_ASR_MODEL', 'whisper-large-v3-turbo')
DEVICE = os.environ.get('TRANSUB_DEVICE', 'cpu')
MT_MODEL = os.environ.get('TRANSUB_MT_MODEL', 'qwen3-4b')
TARGET_LANGUAGES = {'zh'}
TASKS = {'transcribe', 'translate', 'dual'}
MAX_BYTES = int(os.environ.get('TRANSUB_MAX_UPLOAD_MB', '2048')) * 1024 * 1024
RETENTION = 7 * 86400
EXTENSIONS = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.mpeg', '.mpg', '.ts', '.mp3', '.wav', '.flac', '.m4a', '.ogg', '.aac'}
LANGUAGES = {'auto', 'zh', 'en', 'ja', 'ko', 'fr', 'de', 'es', 'ru'}
queue: asyncio.Queue
slots: asyncio.Semaphore


def job_dir(job_id):
    try:
        if str(UUID(job_id)) != job_id:
            raise ValueError()
    except ValueError:
        raise HTTPException(404, '任务不存在')
    return DATA / job_id


def read_job(job_id):
    try:
        return json.loads((job_dir(job_id) / 'job.json').read_text())
    except FileNotFoundError:
        raise HTTPException(404, '任务不存在或已过期')


def save_job(job):
    folder = job_dir(job['id'])
    tmp = folder / 'job.tmp'
    tmp.write_text(json.dumps(job, ensure_ascii=False))
    tmp.replace(folder / 'job.json')


def prune():
    for folder in DATA.iterdir():
        if not folder.is_dir() or folder.is_symlink():
            continue
        try:
            UUID(folder.name)
        except ValueError:
            continue
        if time.time() - folder.stat().st_mtime > RETENTION:
            shutil.rmtree(folder)
    engine_jobs = Path(os.environ.get('TRANSUB_ENGINE_DATA', ROOT / 'engine-data')) / 'jobs'
    for snapshot in engine_jobs.glob('*.json'):
        if time.time() - snapshot.stat().st_mtime > RETENTION:
            snapshot.unlink()


def append_log(job, message, *, stage='', level='info', timestamp=None):
    logs = job.setdefault('logs', [])
    entry = {'ts': timestamp or time.time(), 'level': level, 'stage': stage, 'message': str(message)[:2000]}
    if logs and all(logs[-1][key] == entry[key] for key in ('level', 'stage', 'message')):
        return
    logs.append(entry)
    # shortcut: retain the latest 500 task events; add paged storage if full long-video logs are needed.
    del logs[:-500]
    save_job(job)


def desktop_runtime_ready():
    executable = ROOT.parent / 'node_modules/electron/dist' / ('electron.exe' if sys.platform == 'win32' else 'electron')
    return executable.is_file() and (sys.platform == 'win32' or bool(os.environ.get('DISPLAY')) or shutil.which('xvfb-run') is not None)


def local_translation_ready():
    manifest = ROOT / 'models.json'
    if not manifest.exists():
        return False
    models = json.loads(manifest.read_text())
    model = models.get(MT_MODEL, {})
    model_path = ROOT / 'desktop-data/advanced-llm/models' / model.get('fileName', 'missing')
    runtime = ROOT / 'desktop-data/advanced-llm/runtime'
    return model_path.is_file() and model_path.stat().st_size == model.get('sizeBytes') and any(runtime.rglob('llama-server.exe' if sys.platform == 'win32' else 'llama-server'))


async def stop_pipeline_process(process):
    if os.name == 'posix':
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.returncode is None:
        killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(process.pid), '/T', '/F',
                                                       stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await killer.wait()
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except asyncio.TimeoutError:
        if os.name == 'posix':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        await process.wait()
    finally:
        if os.name == 'posix':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def run_desktop_pipeline(job):
    folder = job_dir(job['id'])
    request = {
        'task': job.get('task', 'transcribe'), 'language': job['language'],
        'targetLanguage': job.get('targetLanguage', 'zh'), 'device': DEVICE,
        'asrModel': MODEL, 'mtModel': MT_MODEL, 'engineUrl': ENGINE,
        'mediaPath': str(folder / ('input' + job['extension'])),
        'outputDir': str(folder / 'output'), 'ffmpegPath': shutil.which('ffmpeg'),
    }
    request_path = folder / 'pipeline-request.json'
    request_path.write_text(json.dumps(request))
    executable = ROOT.parent / 'node_modules/electron/dist' / ('electron.exe' if sys.platform == 'win32' else 'electron')
    command = [str(executable), '--no-sandbox', str(ROOT / 'desktop-worker.cjs'), str(request_path)]
    if sys.platform != 'win32' and not os.environ.get('DISPLAY'):
        command = ['xvfb-run', '-a', *command]
    env = {k: v for k, v in os.environ.items() if k != 'ELECTRON_RUN_AS_NODE'}
    process = await asyncio.create_subprocess_exec(*command, cwd=ROOT.parent, env=env,
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=1024*1024,
                                                  start_new_session=os.name == 'posix')
    result = None
    diagnostic = []
    try:
        async for raw in process.stdout:
            line = raw.decode('utf-8', errors='replace').strip()
            if not line.startswith('TRANSUB_WEB:'):
                diagnostic.append(line)
                diagnostic = diagnostic[-20:]
                continue
            event = json.loads(line.removeprefix('TRANSUB_WEB:'))
            if event['kind'] == 'result':
                result = event['result']
            elif event['kind'] == 'error':
                raise RuntimeError(event['error'])
            elif event['kind'] == 'event':
                payload = event.get('payload') or {}
                if isinstance(payload, str):
                    append_log(job, payload, stage='desktop')
                    continue
                detail = payload.get('line') or payload.get('itemDetail') or payload.get('detail') or payload.get('error')
                stage = payload.get('itemStage') or payload.get('stage') or 'desktop'
                if 'progress' in event.get('channel', ''):
                    job['progress'] = {'stage': stage, 'detail': detail or stage,
                                       'percent': payload.get('itemProgress', payload.get('percent'))}
                if detail:
                    append_log(job, detail, stage=stage, level='error' if payload.get('error') else 'info')
        code = await process.wait()
        if code != 0 or not result or not result.get('ok') or result.get('failed'):
            detail = (result or {}).get('error') or next((r.get('error') for r in (result or {}).get('results', []) if not r.get('ok')), None)
            raise RuntimeError(detail or '\n'.join(diagnostic) or '官方处理流程执行失败')
        return result
    finally:
        await stop_pipeline_process(process)


def pipeline_outputs(job, result):
    folder = job_dir(job['id'])
    output_root = (folder / 'output').resolve()
    outputs = []
    roles = [('sourceSubtitlePath', '原文字幕'), ('targetSubtitlePath', '译文字幕'),
             ('bilingualSubtitlePath', '双语字幕'), ('subtitlePath', '字幕')]
    for item in result.get('results', []):
        for key, label in roles:
            value = item.get(key)
            if not value:
                continue
            path = Path(value).resolve()
            if not path.is_relative_to(output_root) or not path.is_file() or path.suffix.lower() not in {'.srt', '.ass', '.vtt', '.lrc'}:
                continue
            if all(output['path'] != str(path) for output in outputs):
                outputs.append({'path': str(path), 'label': label, 'format': path.suffix[1:]})
    report = output_root / 'pipeline-report.json'
    if report.is_file():
        outputs.append({'path': str(report), 'label': '处理与质检报告', 'format': 'json'})
    if not any(output['format'] == 'srt' for output in outputs):
        raise RuntimeError('官方流程没有生成可下载的 SRT 字幕')
    return outputs


async def worker():
    while True:
        job = await queue.get()
        try:
            job.update(status='running', progress={'detail': '启动官方免费版处理流程'})
            append_log(job, f'官方免费流程 · ASR {MODEL} · 本地翻译 {MT_MODEL} · 设备 {DEVICE}', stage='start')
            result = await run_desktop_pipeline(job)
            outputs = pipeline_outputs(job, result)
            first = next(output for output in outputs if output['format'] == 'srt' and output['label'] == '译文字幕') if any(output['format'] == 'srt' and output['label'] == '译文字幕' for output in outputs) else next(output for output in outputs if output['format'] == 'srt')
            job.update(status='done', subtitle=first['path'], outputs=outputs,
                       qcIssueCount=sum(int(item.get('qcIssueCount') or 0) for item in result.get('results', [])))
            append_log(job, f"处理完成 · 质检剩余 {job['qcIssueCount']} 项 · 字幕可以下载", stage='done')
        except Exception as error:
            job.update(status='error', error=str(error))
            append_log(job, str(error), stage='error', level='error')
        finally:
            (job_dir(job['id']) / ('input' + job['extension'])).unlink(missing_ok=True)
            slots.release()
            queue.task_done()


@asynccontextmanager
async def lifespan(app):
    global queue, slots
    queue = asyncio.Queue(maxsize=8)
    slots = asyncio.Semaphore(8)
    DATA.mkdir(parents=True, exist_ok=True)
    prune()
    for path in DATA.glob('*/job.json'):
        job = json.loads(path.read_text())
        if job['status'] in {'queued', 'running'}:
            job.update(status='error', error='服务已重启，请重新上传')
            append_log(job, job['error'], stage='error', level='error')
            (path.parent / ('input' + job['extension'])).unlink(missing_ok=True)
    task = asyncio.create_task(worker())
    app.state.worker = task
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logging.getLogger(__name__).exception('Task worker stopped; restart after fixing storage/runtime errors')


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


def worker_ready():
    task = getattr(app.state, 'worker', None)
    return task is not None and not task.done()


@app.middleware('http')
async def upload_boundary(request: Request, call_next):
    reserve = request.method == 'POST' and request.url.path == '/api/jobs'
    if request.method == 'POST':
        origin = request.headers.get('origin')
        if origin and origin != f'{request.url.scheme}://{request.headers.get("host")}':
            return JSONResponse({'detail': '不允许跨站上传'}, status_code=403)
        if 'transfer-encoding' in request.headers:
            return JSONResponse({'detail': '请使用带 Content-Length 的文件上传'}, status_code=400)
        try:
            size = int(request.headers.get('content-length', '0'))
        except ValueError:
            return JSONResponse({'detail': '无效的上传长度'}, status_code=400)
        if size <= 0:
            return JSONResponse({'detail': '请提供上传长度'}, status_code=411)
        if size > MAX_BYTES + 1024 * 1024:
            return JSONResponse({'detail': '文件超过上传限制'}, status_code=413)
    if reserve:
        if not worker_ready():
            return JSONResponse({'detail': '任务工作进程已停止，请检查服务日志并重启'}, status_code=503)
        if slots.locked():
            return JSONResponse({'detail': '任务队列已满，请稍后再试'}, status_code=429)
        await slots.acquire()
        request.state.upload_slot_reserved = True
    try:
        response = await call_next(request)
    finally:
        if reserve and request.state.upload_slot_reserved:
            slots.release()
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/', response_class=HTMLResponse)
async def index():
    return (ROOT / 'index.html').read_text()


@app.get('/api/health')
async def health():
    if not worker_ready():
        return {'ok': False, 'message': '任务工作进程已停止，请检查服务日志并重启', 'model': MODEL}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            r = await client.get(ENGINE + '/v1/health')
            r.raise_for_status()
            engine = r.json()
            models = (await client.get(ENGINE + '/v1/models')).json()
        ready = any(m.get('id') == MODEL and m.get('installed') for m in models.get('models', []))
        return {'ok': bool(engine.get('ok')) and ready and not engine.get('stub') and desktop_runtime_ready(), 'model': MODEL, 'device': DEVICE, 'mtModel': MT_MODEL, 'translationReady': local_translation_ready(), 'maxUploadMB': MAX_BYTES // 1024 // 1024}
    except Exception:
        return {'ok': False, 'message': '字幕引擎未就绪', 'model': MODEL}


@app.post('/api/jobs', status_code=202)
async def upload(request: Request, file: UploadFile = File(...), language: str = Form('auto'), task: str = Form('transcribe'), targetLanguage: str = Form('zh')):
    extension = Path(file.filename or '').suffix.lower()
    if extension not in EXTENSIONS or language not in LANGUAGES or task not in TASKS or targetLanguage not in TARGET_LANGUAGES:
        await file.close()
        raise HTTPException(400, '请选择支持的音视频文件和语言')
    if task != 'transcribe' and not local_translation_ready():
        await file.close()
        raise HTTPException(503, '本地翻译模型尚未就绪，请选择原文转写或等待模型安装完成')
    folder = DATA / str(uuid4())
    try:
        prune()
        folder.mkdir()
        size = 0
        with (folder / ('input' + extension)).open('wb') as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, '文件超过上传限制')
                target.write(chunk)
        if size == 0:
            raise HTTPException(400, '文件为空')
        probe = await asyncio.create_subprocess_exec('ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', str(folder / ('input' + extension)), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            stdout, _ = await asyncio.wait_for(probe.communicate(), timeout=30)
        except asyncio.TimeoutError:
            probe.kill()
            await probe.wait()
            raise HTTPException(400, '无法读取媒体文件')
        if probe.returncode or not any(s.get('codec_type') == 'audio' for s in json.loads(stdout).get('streams', [])):
            raise HTTPException(400, '文件无有效音轨，无法生成字幕')
        job = {'id': folder.name, 'name': Path(file.filename).name, 'extension': extension, 'language': language, 'task': task, 'targetLanguage': targetLanguage, 'status': 'queued', 'created': time.time(), 'progress': {}}
        append_log(job, '上传完成，等待处理', stage='queued')
        queue.put_nowait(job)
        request.state.upload_slot_reserved = False
        return {'id': job['id'], 'status': 'queued'}
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        await file.close()


@app.get('/api/jobs/{job_id}')
async def status(job_id: str):
    job = read_job(job_id)
    visible = {k: v for k, v in job.items() if k not in {'subtitle', 'engine_id', 'extension', 'outputs'}}
    visible['outputs'] = [{k: v for k, v in output.items() if k != 'path'} for output in job.get('outputs', [])]
    return visible


@app.get('/api/jobs/{job_id}/subtitle')
async def download(job_id: str):
    job = read_job(job_id)
    if job['status'] != 'done':
        raise HTTPException(409, '字幕尚未生成')
    subtitle = Path(job['subtitle']).resolve()
    if not subtitle.is_relative_to(job_dir(job_id) / 'output') or not subtitle.is_file():
        raise HTTPException(404, '字幕文件不存在')
    return FileResponse(subtitle, media_type='application/x-subrip', filename=Path(job['name']).stem + '.srt')


@app.get('/api/jobs/{job_id}/files/{index}')
async def download_output(job_id: str, index: int):
    job = read_job(job_id)
    outputs = job.get('outputs', [])
    if job['status'] != 'done' or index < 0 or index >= len(outputs):
        raise HTTPException(404, '文件不存在或尚未完成')
    output = outputs[index]
    path = Path(output['path']).resolve()
    if not path.is_relative_to(job_dir(job_id) / 'output') or not path.is_file():
        raise HTTPException(404, '文件不存在')
    filename = Path(job['name']).stem + '.' + output['label'] + path.suffix
    return FileResponse(path, media_type='application/octet-stream', filename=filename)
