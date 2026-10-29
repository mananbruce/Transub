import asyncio
import io
import json
import errno
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave
from uuid import uuid4
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
import app as web


def audio():
    value = io.BytesIO()
    with wave.open(value, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(b'\0\0' * 1600)
    return value.getvalue()


async def pipeline(job):
    output = web.job_dir(job['id']) / 'output/input.srt'
    output.parent.mkdir(exist_ok=True)
    output.write_text('1\n00:00:00,000 --> 00:00:01,000\nTest\n')
    return {'ok': True, 'results': [{'sourceSubtitlePath': str(output), 'qcIssueCount': 0}]}


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = patch.object(web, 'DATA', Path(self.temp.name)); self.data.start()
        self.pipeline = patch.object(web, 'run_desktop_pipeline', side_effect=pipeline); self.pipeline.start()

    def tearDown(self):
        self.pipeline.stop(); self.data.stop(); self.temp.cleanup()

    def create(self, client, size, name='test.wav'):
        response = client.post('/api/uploads', json={'name':name,'size':size,'fingerprint':'test','task':'transcribe'})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def put(self, client, job_id, data, offset):
        return client.put('/api/uploads/'+job_id, content=data, headers={'upload-offset':str(offset),'content-type':'application/octet-stream'})

    def done(self, client, job_id):
        for _ in range(100):
            state = client.get('/api/jobs/'+job_id).json()
            if state['status'] in {'done','error'}: return state
            time.sleep(.01)
        self.fail('job did not finish')

    def test_small_chunks_complete_and_duplicate_submission(self):
        data=audio()
        with TestClient(web.app) as client:
            job_id=self.create(client,len(data))
            self.assertEqual(client.post(f'/api/uploads/{job_id}/complete',json={}).status_code,409)
            for offset in range(0,len(data),1024):
                response=self.put(client,job_id,data[offset:offset+1024],offset)
                self.assertEqual(response.status_code,200,response.text)
            self.assertEqual((web.job_dir(job_id)/'input.wav').read_bytes(),data)
            self.assertEqual(client.post(f'/api/uploads/{job_id}/complete',json={}).status_code,202)
            self.assertEqual(self.done(client,job_id)['status'],'done')
            self.assertEqual(client.post(f'/api/uploads/{job_id}/complete',json={}).status_code,202)
            self.assertEqual(client.get(f'/api/jobs/{job_id}/subtitle').status_code,200)
            self.assertFalse((web.job_dir(job_id)/'input.wav').exists())

    def test_twenty_gib_offset_without_allocating_twenty_gib(self):
        size=20*1024**3
        with TestClient(web.app) as client:
            job_id=self.create(client,size,'large.mp4'); source=web.job_dir(job_id)/'input.mp4'
            with source.open('r+b') as target: target.truncate(size-3)
            web.active[job_id]['received']=size-3; web.save_job(web.active[job_id])
            response=self.put(client,job_id,b'end',size-3)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['received'],size); self.assertEqual(source.stat().st_size,size)
            if hasattr(source.stat(),'st_blocks'): self.assertLess(source.stat().st_blocks*512,1024**2)
            self.assertEqual(client.delete('/api/uploads/'+job_id).status_code,200)
            self.assertFalse(source.exists())

    def test_bounds_and_capacity_released_on_cancel(self):
        with TestClient(web.app) as client:
            self.assertEqual(client.post('/api/uploads',json={'name':'a.mp4','size':web.MAX_BYTES+1}).status_code,413)
            first=self.create(client,8)
            self.assertEqual(self.put(client,first,b'wrong',3).status_code,409)
            self.assertEqual(self.put(client,first,b'waytoolong',0).status_code,413)
            with patch.object(web,'CHUNK_BYTES',2): self.assertEqual(self.put(client,first,b'123',0).status_code,413)
            self.create(client,8)
            self.assertEqual(client.post('/api/uploads',json={'name':'a.wav','size':8}).status_code,429)
            self.assertEqual(client.delete('/api/uploads/'+first).status_code,200); self.create(client,8)

    def test_disk_reservations_include_other_uploads(self):
        free=web.MIN_FREE+2*web.WORKSPACE+199
        with TestClient(web.app) as client,patch.object(web.shutil,'disk_usage',return_value=SimpleNamespace(free=free)):
            self.create(client,100)
            response=client.post('/api/uploads',json={'name':'b.wav','size':100})
            self.assertEqual(response.status_code,507,response.text); self.assertEqual(len(web.active),1)

    def test_disconnect_and_actual_length_roll_back_only_current_chunk(self):
        with TestClient(web.app) as client:
            job_id=self.create(client,8); self.assertEqual(self.put(client,job_id,b'ab',0).status_code,200)
            source=web.job_dir(job_id)/'input.wav'
            async def request(messages,length):
                async def receive(): return messages.pop(0)
                scope={'type':'http','method':'PUT','path':'/','headers':[(b'upload-offset',b'2'),(b'content-length',str(length).encode())]}
                await web.put_chunk(job_id,Request(scope,receive))
            with self.assertRaises(ClientDisconnect):
                asyncio.run(request([{'type':'http.request','body':b'cd','more_body':True},{'type':'http.disconnect'}],4))
            self.assertEqual(source.read_bytes(),b'ab'); self.assertEqual(client.get('/api/uploads/'+job_id).json()['received'],2)
            with self.assertRaises(HTTPException):
                asyncio.run(request([{'type':'http.request','body':b'long','more_body':False}],2))
            self.assertEqual(source.read_bytes(),b'ab'); self.assertEqual(self.put(client,job_id,b'cdefgh',2).status_code,200)

    def test_restart_keeps_upload_and_truncates_unacknowledged_bytes(self):
        with TestClient(web.app) as client:
            job_id=self.create(client,8); self.assertEqual(self.put(client,job_id,b'abcd',0).status_code,200)
        source=web.job_dir(job_id)/'input.wav'
        with source.open('ab') as target: target.write(b'unacknowledged')
        with TestClient(web.app) as client:
            self.assertEqual(source.read_bytes(),b'abcd'); self.assertEqual(client.get('/api/uploads/'+job_id).json()['received'],4)
            self.assertEqual(self.put(client,job_id,b'efgh',4).status_code,200)

    def test_stalled_chunk_times_out_and_keeps_checkpoint(self):
        with TestClient(web.app) as client:
            job_id=self.create(client,8)
            async def stalled():
                async def receive():
                    await asyncio.sleep(5)
                    return {'type':'http.request','body':b'ab','more_body':False}
                scope={'type':'http','method':'PUT','path':'/','headers':[(b'upload-offset',b'0'),(b'content-length',b'2')]}
                await web.put_chunk(job_id,Request(scope,receive))
            with patch.object(web,'CHUNK_TIMEOUT',.01),self.assertRaises(HTTPException) as failure:
                asyncio.run(stalled())
            self.assertEqual(failure.exception.status_code,408)
            self.assertEqual(client.get('/api/uploads/'+job_id).json()['received'],0)
            self.assertEqual(self.put(client,job_id,b'ab',0).status_code,200)

    def test_restart_low_disk_preserves_source_without_starting_pipeline(self):
        data=audio()
        async def long_job(job): await asyncio.sleep(60)
        with patch.object(web,'run_desktop_pipeline',side_effect=long_job):
            with TestClient(web.app) as client:
                job_id=self.create(client,len(data)); self.put(client,job_id,data,0)
                self.assertEqual(client.post(f'/api/uploads/{job_id}/complete',json={}).status_code,202)
        with patch.object(web.shutil,'disk_usage',return_value=SimpleNamespace(free=web.MIN_FREE-1)),patch.object(web,'run_desktop_pipeline') as runner:
            with TestClient(web.app) as client:
                state=self.done(client,job_id)
                self.assertEqual(state['status'],'error'); self.assertTrue(state['canRetry'])
                runner.assert_not_called()
                self.assertEqual((web.job_dir(job_id)/'input.wav').read_bytes(),data)

    def test_cancel_is_idempotent_and_can_clear_failed_source(self):
        with TestClient(web.app) as client:
            job_id=self.create(client,8); self.put(client,job_id,b'12345678',0)
            self.assertEqual(client.delete('/api/uploads/'+job_id).status_code,200)
            self.assertEqual(client.delete('/api/uploads/'+job_id).status_code,200)
            data=audio(); job_id=self.create(client,len(data)); self.put(client,job_id,data,0)
            with patch.object(web,'run_desktop_pipeline',side_effect=RuntimeError('failed')):
                client.post(f'/api/uploads/{job_id}/complete',json={}); self.assertEqual(self.done(client,job_id)['status'],'error')
            self.assertEqual(client.delete('/api/uploads/'+job_id).status_code,200)
            self.assertFalse((web.job_dir(job_id)/'input.wav').exists())

    def test_create_can_be_retried_with_same_id_even_at_capacity(self):
        with TestClient(web.app) as client:
            upload_id=str(uuid4()); spec={'name':'test.wav','size':8,'fingerprint':'same','task':'transcribe'}
            first=client.post('/api/uploads',json=spec,headers={'Upload-ID':upload_id})
            self.assertEqual(first.status_code,201); self.assertEqual(first.json()['id'],upload_id)
            self.put(client,upload_id,b'abcd',0); self.create(client,8)
            retry=client.post('/api/uploads',json=spec,headers={'Upload-ID':upload_id})
            self.assertEqual(retry.status_code,201); self.assertEqual(retry.json()['received'],4)
            self.assertEqual(len(web.active),2)
            self.assertEqual(client.post('/api/uploads',json=dict(spec,size=9),headers={'Upload-ID':upload_id}).status_code,409)

    def test_cancel_persistence_failure_keeps_upload_recoverable(self):
        with TestClient(web.app) as client:
            job_id=self.create(client,8); self.put(client,job_id,b'abcd',0)
            with patch.object(web,'save_job',side_effect=OSError('transient')),self.assertRaises(OSError):
                client.delete('/api/uploads/'+job_id)
            self.assertEqual(client.get('/api/uploads/'+job_id).json()['status'],'uploading')
            self.assertIn(job_id,web.active)
            self.assertEqual((web.job_dir(job_id)/'input.wav').read_bytes(),b'abcd')
            self.assertEqual(self.put(client,job_id,b'efgh',4).status_code,200)
            self.assertEqual(client.delete('/api/uploads/'+job_id).status_code,200)

    def test_chunk_metadata_failure_rolls_back_bytes(self):
        with TestClient(web.app) as client:
            job_id=self.create(client,8)
            with patch.object(web,'save_job',side_effect=OSError(errno.ENOSPC,'full')):
                response=self.put(client,job_id,b'abcd',0)
                self.assertEqual(response.status_code,507)
            self.assertEqual((web.job_dir(job_id)/'input.wav').stat().st_size,0)
            self.assertEqual(client.get('/api/uploads/'+job_id).json()['received'],0)
            self.assertEqual(self.put(client,job_id,b'abcd',0).status_code,200)

    def test_expiry_frees_partial_files_and_slot(self):
        with TestClient(web.app) as client:
            job_id=self.create(client,100); self.put(client,job_id,b'partial',0)
            web.active[job_id]['updated']=time.time()-web.UPLOAD_TTL-1; web.expire_uploads()
            self.assertFalse(web.job_dir(job_id).exists()); self.assertNotIn(job_id,web.active); self.create(client,100)

    def test_processing_failure_can_retry_without_reupload(self):
        data=audio()
        with TestClient(web.app) as client:
            job_id=self.create(client,len(data)); self.put(client,job_id,data,0)
            with patch.object(web,'run_desktop_pipeline',side_effect=RuntimeError('GPU failed')):
                self.assertEqual(client.post(f'/api/uploads/{job_id}/complete',json={}).status_code,202)
                state=self.done(client,job_id); self.assertEqual(state['status'],'error'); self.assertTrue(state['canRetry'])
                self.assertEqual((web.job_dir(job_id)/'input.wav').read_bytes(),data)
            self.assertEqual(client.post(f'/api/jobs/{job_id}/retry',json={}).status_code,202)
            self.assertEqual(self.done(client,job_id)['status'],'done'); self.assertFalse((web.job_dir(job_id)/'input.wav').exists())

    def test_restart_recovers_legacy_complete_source(self):
        job_id=str(uuid4()); folder=web.job_dir(job_id); folder.mkdir()
        (folder/'input.wav').write_bytes(audio())
        (folder/'job.json').write_text(json.dumps({'id':job_id,'name':'test.wav','extension':'.wav','language':'auto','status':'queued','created':time.time(),'progress':{}}))
        with TestClient(web.app) as client:
            self.assertEqual(self.done(client,job_id)['status'],'done')
            self.assertFalse((folder/'input.wav').exists())

    def test_service_restart_requeues_complete_media(self):
        data=audio()
        async def long_job(job): await asyncio.sleep(60)
        with patch.object(web,'run_desktop_pipeline',side_effect=long_job):
            with TestClient(web.app) as client:
                job_id=self.create(client,len(data)); self.put(client,job_id,data,0)
                self.assertEqual(client.post(f'/api/uploads/{job_id}/complete',json={}).status_code,202)
        self.assertTrue((web.job_dir(job_id)/'input.wav').is_file())
        with TestClient(web.app) as client:
            self.assertEqual(self.done(client,job_id)['status'],'done'); self.assertFalse((web.job_dir(job_id)/'input.wav').exists())


if __name__=='__main__': unittest.main()
