// Run the official free desktop pipeline without opening a window.
const fs = require('fs');
const path = require('path');
const { app } = require('electron');
const root = path.resolve(__dirname, '..');
const dataRoot = path.join(path.dirname(path.resolve(process.argv.at(-1))), 'desktop');
process.env.PORTABLE_EXECUTABLE_DIR = dataRoot;
// The service must retain free-tier gates even when running an unpackaged checkout.
process.env.TRANSUB_ADVANCED_DEV_UNLOCK = '0';
fs.mkdirSync(dataRoot, { recursive: true });
const sharedModels = path.join(__dirname, 'desktop-data', 'advanced-llm');
const modelLink = path.join(dataRoot, 'advanced-llm');
if (!fs.existsSync(modelLink)) fs.symlinkSync(sharedModels, modelLink, process.platform === 'win32' ? 'junction' : 'dir');
app.setPath('userData', path.join(dataRoot, 'electron'));
app.commandLine.appendSwitch('disable-gpu');
const emit = (event) => process.stdout.write('TRANSUB_WEB:' + JSON.stringify(event) + '\n');

app.whenReady().then(async () => {
    const request = JSON.parse(fs.readFileSync(process.argv.at(-1), 'utf8'));
    if (!['transcribe', 'translate', 'dual'].includes(request.task)) throw new Error('Invalid task');
    const device = request.device || 'cpu';
    const options = {
        engineUrl: request.engineUrl,
        engineInstallPath: path.join(root, 'transub-engine'),
        engineAutoStart: false,
        engineAsrModel: request.asrModel,
        engineMtModel: request.mtModel || 'qwen3-4b',
        task: request.task,
        language: request.language,
        subtitleTargetLang: request.targetLanguage || 'zh',
        targetLanguage: request.targetLanguage || 'zh',
        chineseSubtitleVariant: 'simplified',
        device,
        nGpuLayers: device === 'cpu' ? 0 : 99,
        translateMode: 'llm',
        smartTranslate: false,
        smartTranslatePlotPolish: false,
        smartTranslateFaithfulVerify: false,
        smartTranslateAddressConsistency: false,
        asrSemanticRepair: 'off',
        filmAudioEnhance: false,
        filmVadPreset: false,
        asrSecondOpinion: 'on',
        qualityPipelineMode: 'on',
        postQcLoopMaxPasses: 1,
        postBatchQc: true,
        postBatchQcFixMode: 'fix',
        postBatchRemoveNoise: true,
        postBatchCompressRepetition: true,
        postBatchCpsSplit: true,
        postBatchViewingPunctMode: 'light',
        postBatchInterjectionMode: 'light',
        postBatchOnomatopoeiaMode: 'light',
        engineVadModel: 'silero-vad',
        includeWords: true,
        outputMode: 'custom',
        outputDir: request.outputDir,
        transcriptKeepDir: path.join(request.outputDir, 'source'),
        subFormats: 'srt',
        dualAss: request.task === 'dual',
        mergeBilingualSubtitles: request.task === 'dual',
        overwrite: true,
        ffmpegPath: request.ffmpegPath || process.env.FFMPEG_BINARY || 'ffmpeg',
        releaseGpuAfter: false,
        asrBadPoolDrafts: false,
        autoShutdown: false,
        afterBatchAction: 'none',
    };
    const sender = {
        id: -1,
        isDestroyed: () => false,
        send: (channel, payload) => {
            if (channel === 'transwithai-progress' || channel === 'transwithai-infer-log') emit({ kind: 'event', channel, payload });
        },
    };
    emit({ kind: 'start', options });
    const gates = require('../electron/advanced-gates');
    if (gates.isDevUnlockEnabled()) throw new Error('Free pipeline must not enable Pro developer unlock');
    const { runEngineBatch } = require('../electron/engine-bridge');
    const result = await runEngineBatch({
        items: [{ fullPath: request.mediaPath }], options, invokeSender: sender,
    });
    fs.mkdirSync(request.outputDir, { recursive: true });
    fs.writeFileSync(path.join(request.outputDir, 'pipeline-report.json'), JSON.stringify(result, null, 2));
    const compact = { ...result, results: (result.results || []).map(({ result: engineResult, ...item }) => item) };
    process.stdout.write('TRANSUB_WEB:' + JSON.stringify({ kind: 'result', result: compact }) + '\n', () => app.exit(result.ok ? 0 : 1));
}).catch((error) => {
    process.stdout.write('TRANSUB_WEB:' + JSON.stringify({ kind: 'error', error: error.stack || String(error) }) + '\n', () => app.exit(1));
});
