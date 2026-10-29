# Transub 局域网字幕服务

默认地址：http://127.0.0.1:8766。局域网使用时设置 `TRANSUB_WEB_HOST=0.0.0.0`，其他设备访问 `http://部署电脑的局域网IP:8766`。

Web 层负责上传、串行队列、日志和下载；处理任务直接调用官方 `electron/engine-bridge.runEngineBatch`，复用免费版桌面编排。上游基线：`1b659c9390113690dd17faeddbac3210a86ea2c4`。

## 处理能力

- 原文转写与质检：ASR、词级时间戳、置信度/风险分档、按需低置信二意见、后处理和 QC 一键修复。
- 翻译：上述原文处理后，由本地模型翻译成简体中文，再进行缺译检查/按需补译、QC、最终观影清理。
- 双语：保留原文和译文，并输出合并双语字幕。
- 最终后处理复用官方标点、重复、杂音/幻觉、读速和时间轴等相关逻辑；仅在满足上游触发条件时执行复核和修复，不保证每个视频都触发重识别。
- 下载原文、译文、双语字幕及完整处理/质检 JSON 报告；显示质检剩余问题数。遵循上游免费版的软件署名行为。

当前翻译目标为简体中文。默认 ASR 为 Whisper large-v3-turbo；翻译为官方免费目录中的 Qwen3 4B Instruct 2507 Q4_K_M，全部在本机运行，不使用云端 API。视频结果仍需人工抽查，质检无问题不代表语义一定正确。

Pro 的转录语义校正、智能/混合翻译、全片语境润色、影片理解、双语语义审阅、QC 智能修复及高级 ASS 样式未接入。免费版的规则清理与 Pro 语义润色是不同能力。

## 运行组成

- `.venv`：Python Web/ASR 依赖，见 `web/requirements.txt`。
- `transub-engine/runtime/Lib/site-packages/transub_engine`：上游 Python 引擎源码，监听 `127.0.0.1:8765`。
- `web/desktop-worker.cjs`：每个任务启动一次无窗口 Electron，直接调用官方批处理函数；Linux/WSL 无 DISPLAY 时通过 `xvfb-run` 运行。
- `node_modules/electron/dist`：Electron 44.1.1，提供桌面主进程依赖；安装 npm 依赖时需要 Node.js ≥22.19。引擎编排运行在 Electron 自带的 Node 中。
- `transub-engine/models/asr/whisper-large-v3-turbo/`：ASR 权重。
- `web/desktop-data/advanced-llm/models/`：Qwen GGUF 权重，模型文件名和大小见 `web/models.json`。
- `web/desktop-data/advanced-llm/runtime/llama-b11029/`：本地 llama-server 及其依赖库。当前为官方 b11029 Ubuntu x64 CPU 包。
- `web/run.sh` 同时管理 Python 引擎和 Web，一个 Web worker。没有额外的永久桌面服务。

默认使用 CPU int8 ASR 与 CPU GGUF 翻译。GPU 部署需要匹配驱动/CUDA 及 llama-server GPU 包，并在目标机器实测，不能仅把 `TRANSUB_DEVICE` 改成 `cuda` 就认为完成部署。

安装 npm 依赖可以使用 `npm ci --ignore-scripts` 后运行 `node node_modules/electron/install.js`；另需系统 FFmpeg、ffprobe，以及 Linux/WSL 上的 Xvfb 和 Electron 系统依赖。无需编译桌面 renderer。模型及 llama-server 必须先安装完成，页面健康状态会区分 ASR 和翻译是否就绪。

来源：

- [Qwen GGUF](https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/blob/main/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf)
- [llama.cpp b11029](https://github.com/ggml-org/llama.cpp/releases/tag/b11029)
- Qwen 权重 SHA-256：`2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e`
- Whisper 权重 SHA-256：`e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da`

## 从干净检出启动（Linux / WSL，CPU）

先安装 Node.js ≥22.19、Python 3.10+、FFmpeg（含 ffprobe）、Xvfb、setsid，以及 Electron 所需系统库。下面命令从仓库根目录运行；网络不通时按实际网络配置下载代理或镜像。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r web/requirements.txt
npm ci --ignore-scripts
node node_modules/electron/install.js
mkdir -p web/desktop-data/advanced-llm/models web/desktop-data/advanced-llm/runtime
.venv/bin/python - <<'PYSETUP'
from huggingface_hub import snapshot_download, hf_hub_download
snapshot_download(
    'deepdml/faster-whisper-large-v3-turbo-ct2',
    local_dir='transub-engine/models/asr/whisper-large-v3-turbo',
    allow_patterns=['config.json', 'model.bin', 'tokenizer.json', 'vocabulary.*', 'preprocessor_config.json'],
)
hf_hub_download(
    'bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF',
    'Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf',
    local_dir='web/desktop-data/advanced-llm/models',
)
PYSETUP
transub_archive=$(mktemp)
curl --fail --location https://github.com/ggml-org/llama.cpp/releases/download/b11029/llama-b11029-bin-ubuntu-x64.tar.gz -o "$transub_archive"
tar -xzf "$transub_archive" -C web/desktop-data/advanced-llm/runtime
rm -- "$transub_archive"
./web/run.sh
```

首次下载约 4 GB 模型，加上运行时还需额外磁盘空间。等 `/api/health` 的 `ok` 和 `translationReady` 均为 true 后再提交双语任务。默认仅本机可访问；局域网前台运行使用 `TRANSUB_WEB_HOST=0.0.0.0 ./web/run.sh`。WSL 默认 NAT 模式下，手机访问还需 Windows 端口转发与家庭网络防火墙规则。

## 服务操作

```bash
systemctl --user start transub-web
systemctl --user stop transub-web
systemctl --user restart transub-web
journalctl --user -u transub-web -n 100 --no-pager
```

前台运行 `./web/run.sh`。需要手动启动的 systemd 用户服务时，将仓库放在 `~/Transub`（或调整模板路径），复制 `web/transub-web.service` 到 `~/.config/systemd/user/` 并执行 `systemctl --user daemon-reload`；不需要开机自启时不要执行 enable。模板绑定所有接口，只用于可信局域网。服务和前台命令不要同时运行。

环境变量：`TRANSUB_ASR_MODEL`、`TRANSUB_MT_MODEL`、`TRANSUB_DEVICE`、`CUDA_VISIBLE_DEVICES`、`TRANSUB_WEB_DATA`、`TRANSUB_MAX_UPLOAD_MB`、`TRANSUB_WEB_HOST`；端口在 `run.sh` 中配置。本地模型必须与官方模型目录 ID 对应。

为严格运行免费版，worker 显式设置 `TRANSUB_ADVANCED_DEV_UNLOCK=0`。上游两个门控入口补充了对此显式关闭值的支持；其余默认行为保持一致。另修复了跨平台字幕路径配对和时间轴恢复时硬编码 Pro 署名的问题。

## 文件与日志

单文件最大 2048 MB，最多 8 个上传中/排队/运行任务，串行执行。坏文件、空文件和无音轨直接拒绝。浏览器保存最近 20 个任务编号。

上传原文件在成功或失败后删除。字幕、报告、状态和该任务的桌面缓存/字幕库记录均保存在 `web/data/{uuid}/`，启动或新上传时清理超过 7 天的任务目录。每个任务的 `desktop/advanced-llm` 链接到共享模型，删除任务不会删除模型。引擎检查点在 `web/engine-data/jobs`，同样按 7 天清理。重启会中断未完成任务并提示重新上传。

每个任务保留最近 500 条官方桌面进度/处理日志，每条最多 2000 字符；约每 2.5 秒刷新，支持折叠、自动滚动和刷新恢复。旧任务缺少日志时显示“暂无日志记录”。全局服务日志通过 journalctl 查看。

页面没有账户或跨设备任务列表；任务 UUID 是状态和下载凭据，仅用于可信局域网。不要对外开放 8765 或 llama-server 端口。

## API

- `GET /api/health`：ASR、桌面运行时及本地翻译模型就绪状态。
- `POST /api/jobs`：multipart `file`、`language`、`task`（`transcribe` / `translate` / `dual`）、`targetLanguage`（当前 `zh`），返回 HTTP 202 和任务编号。旧客户端省略 task 时仍为原文转写。
- `GET /api/jobs/{id}`：状态、进度、日志、质检计数和可下载文件描述；不返回本地文件路径。
- `GET /api/jobs/{id}/files/{index}`：下载指定字幕或 JSON 报告。
- `GET /api/jobs/{id}/subtitle`：兼容旧客户端的默认 SRT 下载。

翻译模型未就绪时拒绝翻译任务，可选择原文转写。官方流程失败时显示失败原因，不把基础 ASR 输出冒充完整流程成功。

## 验证

```bash
cd web
../.venv/bin/python -m unittest -v test_app
cd ..
./node_modules/.bin/vitest run tests/advanced-gates-smart.test.js tests/engine-llama-mt.test.js tests/post-batch-pipeline.test.js tests/asr-second-opinion-and-address.test.js tests/item-posttask-handoff.test.js tests/subtitle-software-credit-core.test.js
```

真实端到端验证：通过页面上传带人声的视频，确认日志经历识别、本地翻译与 QC；下载原文/译文/双语字幕与处理报告并抽查语义和时间戳。另检查刷新后恢复下载、手机布局、损坏文件与无音轨提示。不同视频会按官方触发条件跳过不需要的复核/修复阶段。

当前未提供性能或准确率保证，Windows/WSL GPU 需要在目标机器实测。开源 Core 与引擎沿用上游 MIT 许可，模型遵循各自许可，未使用闭源 Pro 算法。模型、缓存、上传文件、日志和测试产物均不随代码提交。
