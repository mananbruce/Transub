"""Small LAN gateway; the engine stays on loopback and owns ASR."""
import asyncio
import errno
from contextlib import asynccontextmanager
import json
import logging
import math
import os
import platform
from pathlib import Path
import shutil
import signal
import sys
import time
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('TRANSUB_WEB_DATA', ROOT / 'data')).resolve()
ENGINE = os.environ.get('TRANSUB_ENGINE_URL', 'http://127.0.0.1:8765').rstrip('/')
MODEL = os.environ.get('TRANSUB_ASR_MODEL', 'whisper-large-v3-turbo')
DEVICE = os.environ.get('TRANSUB_DEVICE', 'cpu')
MT_MODEL = os.environ.get('TRANSUB_MT_MODEL', 'qwen3-4b')
TARGET_LANGUAGES = {'zh'}
TASKS = {'transcribe', 'translate', 'dual'}
MAX_BYTES = int(os.environ.get('TRANSUB_MAX_UPLOAD_MB', '24576')) * 1024 * 1024
LEGACY_MAX_BYTES = min(MAX_BYTES, 2 * 1024**3)
CHUNK_BYTES = 16 * 1024**2
CHUNK_TIMEOUT = 180
CAPACITY = 2
MIN_FREE = 10 * 1024**3
WORKSPACE = 4 * 1024**3
UPLOAD_TTL = 86400
RETENTION = 7 * 86400
EXTENSIONS = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.mpeg', '.mpg', '.ts', '.mp3', '.wav', '.flac', '.m4a', '.ogg', '.aac'}
LANGUAGES = {'auto', 'zh', 'en', 'ja', 'ko', 'fr', 'de', 'es', 'ru'}
queue: asyncio.Queue
slots: asyncio.Semaphore
active: dict = {}
upload_locks: dict = {}


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
        if folder.name in active:
            continue
        if time.time() - folder.stat().st_mtime > RETENTION:
            shutil.rmtree(folder)
    engine_jobs = Path(os.environ.get('TRANSUB_ENGINE_DATA', ROOT / 'engine-data')) / 'jobs'
    for snapshot in engine_jobs.glob('*.json'):
        if time.time() - snapshot.stat().st_mtime > RETENTION:
            snapshot.unlink()


def disk_check(extra=0, exclude=None):
    reserved = sum(max(0, job.get('size', 0) - job.get('received', 0)) + job.get('workspace', WORKSPACE)
                   for key, job in active.items() if key != exclude)
    if shutil.disk_usage(DATA).free < MIN_FREE + reserved + extra:
        raise HTTPException(507, '磁盘空间不足：需要容纳文件、处理临时空间，并保留至少 10 GiB 空闲')


def release_slot(job_id):
    if active.pop(job_id, None) is not None:
        slots.release()
    upload_locks.pop(job_id, None)


def expire_uploads(history=True):
    now = time.time()
    for job_id, job in list(active.items()):
        lock = upload_locks.get(job_id)
        if job['status'] == 'uploading' and now - job.get('updated', job['created']) > UPLOAD_TTL and not (lock and lock.locked()):
            shutil.rmtree(job_dir(job_id))
            release_slot(job_id)
    if not history:
        return
    for source in DATA.glob('*/input.*'):
        if source.parent.is_symlink():
            continue
        try:
            job_dir(source.parent.name)
        except HTTPException:
            continue
        if source.parent.name in active or now - source.parent.stat().st_mtime < UPLOAD_TTL:
            continue
        job = json.loads((source.parent / 'job.json').read_text())
        if job['status'] in {'error', 'cancelled'} and source.name == 'input' + job['extension']:
            source.unlink(missing_ok=True)
    prune()


async def maintenance():
    while True:
        await asyncio.sleep(60)
        try:
            expire_uploads()
        except OSError:
            logging.getLogger(__name__).exception('Cannot clean expired uploads')


async def validate_media(path):
    probe = await asyncio.create_subprocess_exec('ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type:format=duration', '-of', 'json', str(path),
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(probe.communicate(), timeout=60)
    except asyncio.TimeoutError:
        probe.kill()
        await probe.wait()
        raise HTTPException(400, '音轨校验超时，已上传文件会保留，可稍后重试或取消')
    except BaseException:
        if probe.returncode is None:
            probe.kill()
        await probe.wait()
        raise
    try:
        info = json.loads(stdout)
        duration = float(info.get('format', {}).get('duration') or 0)
    except (ValueError, TypeError):
        raise HTTPException(400, '无法读取媒体文件')
    if probe.returncode or not any(stream.get('codec_type') == 'audio' for stream in info.get('streams', [])):
        raise HTTPException(400, '文件无有效音轨，无法生成字幕')
    if not math.isfinite(duration) or duration < 0:
        raise HTTPException(400, '无效的媒体时长')
    return duration


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
    return executable.is_file() and shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None and (sys.platform == 'win32' or bool(os.environ.get('DISPLAY')) or shutil.which('xvfb-run') is not None)


def storage_warning():
    if 'microsoft' in platform.release().lower() and not (str(DATA).startswith('/mnt/') and len(DATA.parts) > 2 and len(DATA.parts[2]) == 1):
        return 'WSL 虚拟磁盘的空闲空间可能大于 Windows 宿主盘实际空闲，请同时检查宿主盘或选择实际数据盘存储'
    return ''


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
    scratch = folder / 'scratch'
    scratch.mkdir(exist_ok=True)
    env.update(TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch))
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
            disk_check(job.get('workspace', WORKSPACE), exclude=job['id'])
            job.update(status='running', progress={'detail': '启动官方免费版处理流程'})
            append_log(job, f'官方免费流程 · ASR {MODEL} · 本地翻译 {MT_MODEL} · 设备 {DEVICE}', stage='start')
            result = await run_desktop_pipeline(job)
            outputs = pipeline_outputs(job, result)
            first = next(output for output in outputs if output['format'] == 'srt' and output['label'] == '译文字幕') if any(output['format'] == 'srt' and output['label'] == '译文字幕' for output in outputs) else next(output for output in outputs if output['format'] == 'srt')
            job.update(status='done', subtitle=first['path'], outputs=outputs,
                       qcIssueCount=sum(int(item.get('qcIssueCount') or 0) for item in result.get('results', [])))
            append_log(job, f"处理完成 · 质检剩余 {job['qcIssueCount']} 项 · 字幕可以下载", stage='done')
        except Exception as error:
            job.update(status='error', error=str(error), updated=time.time())
            append_log(job, str(error), stage='error', level='error')
        finally:
            if job['status'] == 'done':
                (job_dir(job['id']) / ('input' + job['extension'])).unlink(missing_ok=True)
            shutil.rmtree(job_dir(job['id']) / 'scratch', ignore_errors=True)
            release_slot(job['id'])
            queue.task_done()


@asynccontextmanager
async def lifespan(app):
    global queue, slots, active, upload_locks
    queue = asyncio.Queue(maxsize=CAPACITY)
    slots = asyncio.Semaphore(CAPACITY)
    active, upload_locks = {}, {}
    DATA.mkdir(parents=True, exist_ok=True)
    for path in DATA.glob('*/job.json'):
        job = json.loads(path.read_text())
        source = path.parent / ('input' + job['extension'])
        if job['status'] == 'uploading' and time.time() - job.get('updated', job['created']) > UPLOAD_TTL:
            shutil.rmtree(path.parent)
            continue
        if job['status'] in {'uploading', 'queued', 'running'}:
            if job['status'] != 'uploading' and not job.get('size') and source.is_file() and job.get('name') and job.get('language'):
                job.update(size=source.stat().st_size, received=source.stat().st_size, workspace=WORKSPACE)
            received = job.get('received', 0)
            valid = 0 <= received <= job.get('size', 0) <= MAX_BYTES and job.get('size', 0) > 0 and source.is_file() and source.stat().st_size >= received
            if valid and not slots.locked():
                await slots.acquire()
                active[job['id']] = job
                upload_locks[job['id']] = asyncio.Lock()
                if job['status'] == 'uploading':
                    with source.open('r+b') as target:
                        target.truncate(received)
                elif source.stat().st_size == job['size']:
                    job.update(status='queued')
                    append_log(job, '服务已重启，保留源文件并重新排队', stage='queued')
                    queue.put_nowait(job)
                else:
                    release_slot(job['id'])
                    job.update(status='error', error='源文件不完整，请重新上传')
                    save_job(job)
            else:
                job.update(status='error', error='服务重启后恢复队列已满，可稍后重试' if valid else '服务已重启，源文件不完整，请重新上传')
                save_job(job)
                intact = source.is_file() and job.get('size', 0) > 0 and source.stat().st_size == job['size']
                if not valid and not intact:
                    source.unlink(missing_ok=True)
    expire_uploads()
    task = asyncio.create_task(worker())
    cleaner = asyncio.create_task(maintenance())
    app.state.worker = task
    yield
    cleaner.cancel()
    task.cancel()
    for pending in (cleaner, task):
        try:
            await pending
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.getLogger(__name__).exception('Background task stopped; check storage/runtime and restart')


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


def worker_ready():
    task = getattr(app.state, 'worker', None)
    return task is not None and not task.done()


@app.middleware('http')
async def upload_boundary(request: Request, call_next):
    reserve = request.method == 'POST' and request.url.path in {'/api/jobs', '/api/uploads'}
    mutating = request.method in {'POST', 'PUT', 'DELETE'}
    size = 0
    if mutating:
        origin = request.headers.get('origin')
        if origin and origin != f'{request.url.scheme}://{request.headers.get("host")}':
            return JSONResponse({'detail': '不允许跨站上传'}, status_code=403)
    if request.method in {'POST', 'PUT'}:
        if 'transfer-encoding' in request.headers:
            return JSONResponse({'detail': '请使用带 Content-Length 的文件上传'}, status_code=400)
        try:
            size = int(request.headers.get('content-length', '0'))
        except ValueError:
            return JSONResponse({'detail': '无效的上传长度'}, status_code=400)
        limit = LEGACY_MAX_BYTES + 1024**2 if request.url.path == '/api/jobs' else CHUNK_BYTES if request.method == 'PUT' else 16384
        if size <= 0:
            return JSONResponse({'detail': '请提供上传长度'}, status_code=411)
        if size > limit:
            return JSONResponse({'detail': '请求过大；大文件请使用分片上传'}, status_code=413)
    if request.method == 'POST' and request.url.path == '/api/uploads' and request.headers.get('upload-id'):
        try:
            requested_id = job_dir(request.headers['upload-id']).name
        except HTTPException:
            return JSONResponse({'detail': '无效的上传编号'}, status_code=400)
        request.state.task_id = requested_id
        if (job_dir(requested_id) / 'job.json').is_file():
            reserve = False
        elif requested_id in active:
            return JSONResponse({'detail': '上传正在创建，请稍后恢复'}, status_code=409)
    if reserve:
        expire_uploads(history=False)
        if not worker_ready():
            return JSONResponse({'detail': '任务工作进程已停止，请检查服务日志并重启'}, status_code=503)
        if slots.locked():
            return JSONResponse({'detail': '最多同时保留两个上传/排队/处理任务，请先取消未完成上传或等待处理完成'}, status_code=429)
        await slots.acquire()
        task_id = getattr(request.state, 'task_id', None) or str(uuid4())
        request.state.task_id = task_id
        request.state.upload_slot_reserved = True
        active[task_id] = {'id': task_id, 'status': 'receiving', 'size': size * 2 if request.url.path == '/api/jobs' else 0, 'received': 0, 'workspace': WORKSPACE}
        try:
            disk_check()
        except HTTPException as error:
            release_slot(task_id)
            return JSONResponse({'detail': error.detail}, status_code=error.status_code)
    try:
        if reserve and request.url.path == '/api/uploads':
            try:
                response = await asyncio.wait_for(call_next(request), timeout=30)
            except asyncio.TimeoutError:
                response = JSONResponse({'detail': '上传创建超时，请使用原上传编号重试'}, status_code=408)
        else:
            response = await call_next(request)
    finally:
        if reserve and request.state.upload_slot_reserved:
            release_slot(request.state.task_id)
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
        return {'ok': bool(engine.get('ok')) and ready and not engine.get('stub') and desktop_runtime_ready(), 'model': MODEL, 'device': DEVICE, 'mtModel': MT_MODEL, 'translationReady': local_translation_ready(), 'maxUploadMB': MAX_BYTES // 1024 // 1024, 'chunkBytes': CHUNK_BYTES, 'maxTasks': CAPACITY, 'freeDiskBytes': shutil.disk_usage(DATA).free, 'storageWarning': storage_warning()}
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
    folder = DATA / request.state.task_id
    try:
        prune()
        folder.mkdir()
        size = 0
        with (folder / ('input' + extension)).open('wb') as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > LEGACY_MAX_BYTES:
                    raise HTTPException(413, '文件超过上传限制')
                target.write(chunk)
        if size == 0:
            raise HTTPException(400, '文件为空')
        duration = await validate_media(folder / ('input' + extension))
        job = {'id': folder.name, 'name': Path(file.filename).name, 'extension': extension, 'language': language, 'task': task, 'targetLanguage': targetLanguage, 'status': 'queued', 'created': time.time(), 'updated': time.time(), 'size': size, 'received': size, 'workspace': max(WORKSPACE, math.ceil(duration * 64000 * 4)), 'progress': {}}
        disk_check(job['workspace'], exclude=job['id'])
        active[job['id']] = job
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
    source = job_dir(job_id) / ('input' + job['extension'])
    visible['canRetry'] = job['status'] == 'error' and source.is_file() and source.stat().st_size == job.get('size')
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


class UploadSpec(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, strict=True)
    fingerprint: str = Field(default='', max_length=2048)
    language: str = Field(default='auto', max_length=16)
    task: str = Field(default='transcribe', max_length=16)
    targetLanguage: str = Field(default='zh', max_length=16)
    model_config = {'extra': 'forbid'}


def check_options(language, task, target):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise HTTPException(503, '请先安装 FFmpeg 和 ffprobe，再上传大文件')
    if language not in LANGUAGES or task not in TASKS or target not in TARGET_LANGUAGES:
        raise HTTPException(400, '不支持的语言或处理方式')
    if task != 'transcribe' and not local_translation_ready():
        raise HTTPException(503, '本地翻译模型尚未就绪')


@app.post('/api/uploads', status_code=201)
async def create_upload(request: Request, spec: UploadSpec):
    existing = job_dir(request.state.task_id) / 'job.json'
    if existing.is_file():
        job = read_job(request.state.task_id)
        expected = spec.model_dump()
        expected['name'] = Path(spec.name).name
        if any(job.get(key) != value for key, value in expected.items()):
            raise HTTPException(409, '上传编号对应其他文件或处理方式')
        return {'id': job['id'], 'received': job['received'], 'status': job['status'], 'chunkBytes': CHUNK_BYTES}
    extension = Path(spec.name).suffix.lower()
    if extension not in EXTENSIONS:
        raise HTTPException(400, '不支持的音视频文件')
    if spec.size > MAX_BYTES:
        raise HTTPException(413, '文件超过上传上限')
    check_options(spec.language, spec.task, spec.targetLanguage)
    disk_check(spec.size + WORKSPACE, exclude=request.state.task_id)
    folder = job_dir(request.state.task_id)
    try:
        folder.mkdir()
        (folder / ('input' + extension)).touch(exist_ok=False)
        job = dict(spec.model_dump(), id=folder.name, name=Path(spec.name).name, extension=extension,
                   status='uploading', received=0, created=time.time(), updated=time.time(), workspace=WORKSPACE, progress={})
        append_log(job, '开始分片上传，断线后可重新选择同一文件继续', stage='upload')
        active[job['id']] = job
        upload_locks[job['id']] = asyncio.Lock()
        request.state.upload_slot_reserved = False
        return {'id': job['id'], 'received': 0, 'status': 'uploading', 'chunkBytes': CHUNK_BYTES}
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


@app.get('/api/uploads/{job_id}')
async def upload_status(job_id: str):
    job = read_job(job_id)
    return {key: job.get(key) for key in ('id', 'name', 'size', 'received', 'fingerprint', 'status', 'language', 'task', 'targetLanguage')}


def upload_lock(job_id):
    job_dir(job_id)
    job = active.get(job_id)
    if not job or job['status'] != 'uploading':
        raise HTTPException(409, '上传不存在、已过期或已提交，请刷新任务状态')
    lock = upload_locks[job_id]
    if lock.locked():
        raise HTTPException(409, '上一分片仍在处理中，请稍后重试')
    return job, lock


@app.put('/api/uploads/{job_id}')
async def put_chunk(job_id: str, request: Request):
    job, lock = upload_lock(job_id)
    try:
        offset = int(request.headers.get('upload-offset', '-1'))
        length = int(request.headers.get('content-length', '0'))
    except ValueError:
        raise HTTPException(400, '无效的分片位置')
    async with lock:
        start = job['received']
        if offset != start:
            raise HTTPException(409, f'分片位置不匹配，服务器已接收 {start} 字节')
        if not 0 < length <= CHUNK_BYTES or start + length > job['size']:
            raise HTTPException(413, '分片超过文件大小或单片上限')
        disk_check()
        source = job_dir(job_id) / ('input' + job['extension'])
        seen = 0
        with source.open('r+b', buffering=0) as target:
            target.seek(start)
            target.truncate(start)
            try:
                async def receive():
                    nonlocal seen
                    async for chunk in request.stream():
                        if seen + len(chunk) > length:
                            raise HTTPException(413, '分片实际长度超过声明值')
                        remaining = memoryview(chunk)
                        while remaining:
                            written = target.write(remaining)
                            if not written:
                                raise OSError(errno.EIO, 'short file write')
                            remaining = remaining[written:]
                        seen += len(chunk)
                await asyncio.wait_for(receive(), timeout=CHUNK_TIMEOUT)
                if seen != length:
                    raise HTTPException(400, '分片不完整，请重新发送')
                target.flush()
                os.fsync(target.fileno())
                job.update(received=start + seen, updated=time.time())
                save_job(job)
            except BaseException as error:
                job['received'] = start
                target.seek(start)
                target.truncate(start)
                target.flush()
                if isinstance(error, asyncio.TimeoutError):
                    raise HTTPException(408, '分片接收超时，已保留之前的上传断点')
                if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
                    raise HTTPException(507, '磁盘写入失败，请释放磁盘空间后继续上传')
                if isinstance(error, OSError) and error.errno == errno.EFBIG:
                    raise HTTPException(413, '存储文件系统不支持该文件大小，请使用支持大文件的磁盘')
                raise
        return {'id': job_id, 'received': job['received']}


@app.post('/api/uploads/{job_id}/complete', status_code=202)
async def complete_upload(job_id: str):
    previous = read_job(job_id)
    if previous['status'] in {'queued', 'running', 'done'}:
        return {'id': job_id, 'status': previous['status']}
    job, lock = upload_lock(job_id)
    async with lock:
        if not worker_ready():
            raise HTTPException(503, '处理服务未就绪，已上传文件会保留，请稍后重试')
        source = job_dir(job_id) / ('input' + job['extension'])
        if job['received'] != job['size'] or source.stat().st_size != job['size']:
            raise HTTPException(409, '文件尚未上传完成')
        check_options(job['language'], job['task'], job['targetLanguage'])
        duration = await validate_media(source)
        workspace = max(WORKSPACE, math.ceil(duration * 64000 * 4))
        disk_check(workspace, exclude=job_id)
        job.update(status='queued', duration=duration, workspace=workspace, updated=time.time())
        try:
            append_log(job, '上传完成，音轨校验通过，等待处理', stage='queued')
        except BaseException:
            job['status'] = 'uploading'
            raise
        queue.put_nowait(job)
        return {'id': job_id, 'status': 'queued'}


@app.delete('/api/uploads/{job_id}')
async def cancel_upload(job_id: str):
    previous = read_job(job_id)
    source = job_dir(job_id) / ('input' + previous['extension'])
    if previous['status'] == 'cancelled':
        source.unlink(missing_ok=True)
        return {'id': job_id, 'status': 'cancelled'}
    if previous['status'] == 'error' and job_id not in active:
        previous.update(status='cancelled', error=None, updated=time.time())
        save_job(previous)
        source.unlink(missing_ok=True)
        return {'id': job_id, 'status': 'cancelled'}
    job, lock = upload_lock(job_id)
    async with lock:
        old_updated = job['updated']
        job.update(status='cancelled', updated=time.time())
        try:
            save_job(job)
        except BaseException:
            job.update(status='uploading', updated=old_updated)
            raise
        try:
            source.unlink(missing_ok=True)
        finally:
            release_slot(job_id)
        return {'id': job_id, 'status': 'cancelled'}


@app.post('/api/jobs/{job_id}/retry', status_code=202)
async def retry_job(job_id: str):
    job = read_job(job_id)
    source = job_dir(job_id) / ('input' + job['extension'])
    if job['status'] != 'error' or not source.is_file() or source.stat().st_size != job.get('size'):
        raise HTTPException(409, '没有可重试的完整源文件，请重新上传')
    if not worker_ready():
        raise HTTPException(503, '处理服务未就绪')
    check_options(job['language'], job.get('task', 'transcribe'), job.get('targetLanguage', 'zh'))
    if slots.locked():
        raise HTTPException(429, '任务队列已满，请稍后重试')
    disk_check(job.get('workspace', WORKSPACE))
    await slots.acquire()
    try:
        job.update(status='queued', updated=time.time(), error=None)
        append_log(job, '使用已上传的源文件重新处理', stage='queued')
        active[job_id] = job
        queue.put_nowait(job)
    except BaseException:
        slots.release()
        raise
    return {'id': job_id, 'status': 'queued'}
