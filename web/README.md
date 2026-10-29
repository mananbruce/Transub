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

页面默认支持最多 24 GiB 文件（覆盖 20 GB / 20 GiB）。按 16 MiB 分片直接写入同一个目标文件，接收时不创建整份 multipart 临时副本；单片核验长度，写入并同步到磁盘后才确认偏移，断线/异常只回退当前未确认分片。旧 `POST /api/jobs` multipart 接口仍限制 2 GiB，大文件应使用页面的新协议。

最多同时容纳两个上传/排队/运行任务，仍只运行一个识别任务。开始上传前按文件总大小和处理临时空间做逻辑磁盘预留；并发上传、排队等待及服务重启后开始处理前都重新检查，至少保留 10 GiB 空闲。两小时影片默认预留 4 GiB 临时空间，较长媒体根据探测时长增加。一个 20 GiB 文件需要约 34 GiB 实际空闲空间；两个此类任务约需 58 GiB。其他程序仍可能占用磁盘，运行中空间不足会暂停/报错并保留已确认断点，不保证其他软件不会填满磁盘。

上传暂停或页面刷新后，重新选择**同一文件和处理方式/语言**即可续传；断线自动重试至多 3 次后暂停。浏览器只读取首尾少量样本识别所选文件，不做耗时的全文件哈希，续传期间不要修改源视频。上传编号先保存在浏览器，再提交到服务器；创建/完成/取消均能处理回复丢失的重试。手机请保持页面前台，浏览器后台冻结或被系统关闭后需重新选择文件继续。

未完成上传保留 24 小时（按最近确认分片计时），每分钟清理过期残片；可随时取消并删除。完成上传的任务在服务重启后重新排队处理，保留源文件，避免再次传输；处理步骤本身可能从头执行。处理失败的完整源文件保留 24 小时，页面支持免上传重试或主动删除；成功后立即删除上传源文件。字幕、报告、状态和任务的桌面缓存/字幕库记录保留 7 天。任务删除不会沿模型目录链接删除共享模型。

Python 引擎的临时目录位于 `${TRANSUB_WEB_DATA}/.engine-tmp`，Electron 的临时目录位于对应任务目录，避免大音频临时文件意外占用系统 `/tmp` 分区。引擎检查点在 `web/engine-data/jobs`，按 7 天清理。

**Windows / WSL 存储注意：**使用支持大文件的 NTFS 或 ext4，不能使用单文件约 4 GiB 上限的 FAT32。WSL 的 Linux 虚拟磁盘报告空闲量可能大于 Windows 宿主盘实际剩余空间，页面会提示；建议将上传目录直接指向实际 Windows 数据盘，例如 `TRANSUB_WEB_DATA=/mnt/d/TransubData`，并同时检查 Windows 盘剩余空间。仅“磁盘容量 200 GB”不代表当前有 200 GB 空闲。

对于 20 GB、两小时电影，视频体积影响传输和磁盘；ASR 主要处理音频。16kHz 单声道 PCM16 约 230 MB，float32 波形约 461 MB，仍有模型/VAD/翻译上下文的额外开销。32 GB 内存、16 GB 显存属于可用配置，但 GPU 驱动、CUDA、推理后端和长视频效果必须实测，不能由这两个容量直接保证速度或准确率。视频本来就在电脑上时，电脑浏览器上传可避免先把 20 GB 复制到手机；手机可查看进度和下载结果。

页面没有账户或跨设备任务列表；任务 UUID 是状态和下载凭据，仅用于可信局域网。不要对外开放 8765 或 llama-server 端口。

## API

- `GET /api/health`：运行时就绪状态、单文件/分片上限、可用磁盘和 WSL 存储提示。
- `POST /api/uploads`：JSON `name`、`size`、`fingerprint`、`language`、`task`、`targetLanguage` 创建上传。可提供 UUID 格式的 `Upload-ID` 请求头；客户端应在请求前持久保存此编号，同一编号/元数据重试返回原断点。
- `GET /api/uploads/{id}`：查询已确认的 `received` 字节数和状态。
- `PUT /api/uploads/{id}`：原始分片字节，`Content-Length` 不超过 16 MiB，`Upload-Offset` 必须等于服务器断点；超时、超长和未完成分片不会推进偏移。
- `POST /api/uploads/{id}/complete`：JSON `{}`，校验完整文件与音轨后入队，重复提交不重复创建任务。
- `DELETE /api/uploads/{id}`：取消未完成上传，或删除失败任务保留的源文件；不会删除已排队/运行任务的输入。
- `POST /api/jobs/{id}/retry`：JSON `{}`，复用失败任务的完整源文件重新处理。
- `POST /api/jobs`：旧 multipart `file`、`language`、`task`（`transcribe` / `translate` / `dual`）、`targetLanguage`（当前 `zh`），返回 HTTP 202 和任务编号。旧客户端省略 task 时仍为原文转写。
- `GET /api/jobs/{id}`：状态、进度、日志、质检计数和可下载文件描述；不返回本地文件路径。
- `GET /api/jobs/{id}/files/{index}`：下载指定字幕或 JSON 报告。
- `GET /api/jobs/{id}/subtitle`：兼容旧客户端的默认 SRT 下载。

浏览器分片请求使用已知 Content-Length，不支持 HTTP chunked 请求体。反向代理如有请求体大小限制，设为至少单片 16 MiB（及少量请求开销），并避免把整个请求缓存到内存；不需要放开单请求 20 GB。

翻译模型未就绪时拒绝翻译任务，可选择原文转写。官方流程失败时显示失败原因，不把基础 ASR 输出冒充完整流程成功。

## 验证

```bash
cd web
../.venv/bin/python -m unittest -v test_app test_uploads
node test_upload_ui.cjs
cd ..
./node_modules/.bin/vitest run tests/advanced-gates-smart.test.js tests/engine-llama-mt.test.js tests/post-batch-pipeline.test.js tests/asr-second-opinion-and-address.test.js tests/item-posttask-handoff.test.js tests/subtitle-software-credit-core.test.js
```

真实端到端验证：通过页面上传带人声的视频，确认日志经历识别、本地翻译与 QC；下载原文/译文/双语字幕与处理报告并抽查语义和时间戳。另检查刷新后恢复下载、手机布局、损坏文件与无音轨提示。不同视频会按官方触发条件跳过不需要的复核/修复阶段。

当前未提供性能或准确率保证，Windows/WSL GPU 需要在目标机器实测。开源 Core 与引擎沿用上游 MIT 许可，模型遵循各自许可，未使用闭源 Pro 算法。模型、缓存、上传文件、日志和测试产物均不随代码提交。

大文件验证：20 GiB 偏移/容量边界使用稀疏文件验证，另以约 48 MiB 视频通过真实浏览器模拟断线、暂停、刷新续传、取消，并完成官方字幕处理和下载。未使用真实 20 GiB 两小时影片进行全字节传输与推理，也未对用户家用 Windows GPU 实测。
