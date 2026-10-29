// Exercise the real page script's lost-response reconciliation without browser dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;
const source = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8').split('<script>')[1].split('</script>')[0];
const oldId = '00000000-0000-0000-0000-000000000001';
const newId = '00000000-0000-0000-0000-000000000002';

async function scenario(status, sameFile, cancel = false, loseCreation = false) {
    const storage = new Map([['transub-upload', JSON.stringify({ id: oldId, fingerprint: 'different file' })]]);
    const element = () => ({ value: '', textContent: '', hidden: false, disabled: false, scrollHeight: 100, scrollTop: 0,
        querySelector: () => null, append() {}, prepend() {}, replaceChildren() {}, setAttribute() {}, reset() {} });
    const elements = Object.fromEntries(['file','task','language','targetLanguage','submit','health','translationStatus','limit','jobs','form','message','upload','uploadDetail','pause','cancel','storageWarning'].map(id => [id, element()]));
    elements.task.value = 'transcribe'; elements.language.value = 'auto'; elements.targetLanguage.value = 'zh';
    elements.file.files = [Object.assign(new Blob(['abcd']), { name: 'movie.mp4' })];
    let newUploads = 0, creationDropped = false;
    const created = new Set();
    class XHR {
        constructor() { this.upload = {}; }
        open() {} setRequestHeader() {} abort() { this.onabort?.(); }
        send(blob) { this.status = 200; this.responseText = JSON.stringify({ received: blob.size }); queueMicrotask(() => this.onload()); }
    }
    const fetch = async (url, options = {}) => {
        let body, code = 200;
        if (url === '/api/health') body = { ok: true, translationReady: false, maxUploadMB: 24576 };
        else if (url === `/api/uploads/${oldId}` && options.method === 'DELETE') { body = { detail: 'submitted' }; code = 409; }
        else if (url === `/api/uploads/${oldId}`) body = { status, received: 4 };
        else if (url === '/api/uploads') { const id=options.headers['Upload-ID']; assert.equal(JSON.parse(storage.get('transub-upload')).id,id); if(!created.has(id))newUploads++;created.add(id);if(loseCreation&&!creationDropped){creationDropped=true;throw Error('lost create response')}body = { id, chunkBytes:16*1024**2 };code=201; }
        else if (url.startsWith('/api/uploads/') && created.has(url.split('/').pop())) body={status:'uploading',received:0};
        else if (url.endsWith('/complete')) { body = { status: 'queued' }; code = 202; }
        else if (url.startsWith('/api/jobs/')) body = { id: url.split('/').pop(), status: 'error', name: 'movie.mp4', logs: [] };
        else throw new Error('Unexpected request: ' + url);
        return new Response(JSON.stringify(body), { status: code });
    };
    const context = vm.createContext({ document: { getElementById: id => elements[id] || null, createElement: element },
        localStorage: { getItem: key => storage.get(key) || null, setItem: (key,value) => storage.set(key,value), removeItem: key => storage.delete(key) },
        crypto, fetch, XMLHttpRequest: XHR, setInterval() {}, setTimeout, queueMicrotask, btoa, performance, console });
    vm.runInContext(source, context);
    await new Promise(resolve => setImmediate(resolve));
    if (sameFile) {
        const fingerprint = await vm.runInContext("identity($('file').files[0],{language:'auto',task:'transcribe',targetLanguage:'zh'})", context);
        vm.runInContext('currentUpload.fingerprint=' + JSON.stringify(fingerprint), context);
    }
    if (cancel) await elements.cancel.onclick();
    else {await elements.form.onsubmit({ preventDefault() {} });if(loseCreation){assert.equal(storage.has('transub-upload'),true);await elements.form.onsubmit({preventDefault(){}})}}
    assert.equal(storage.has('transub-upload'), false, `${status}: stale handle retained`);
    assert.equal(newUploads, cancel || (sameFile && status !== 'cancelled') ? 0 : 1, `${status}: unexpected duplicate or blocked upload`);
    if (cancel) assert.match(elements.message.textContent, /本次未执行/);
}

(async () => {
    await scenario('error', true);
    await scenario('queued', true);
    await scenario('done', true);
    await scenario('cancelled', true);
    await scenario('error', false);
    await scenario('queued', false, true);
    await scenario('cancelled', false, false, true);
    console.log('PASS: seven lost-response and terminal-upload reconciliation cases');
})().catch(error => { console.error(error); process.exitCode = 1; });
