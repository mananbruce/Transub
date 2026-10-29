#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/transub-engine/runtime/Lib/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export TRANSUB_ENGINE_HOME="$PWD/transub-engine"
export TRANSUB_ENGINE_DATA="$PWD/web/engine-data"
export TRANSUB_DEVICE="${TRANSUB_DEVICE:-cpu}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:--1}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"
export TRANSUB_ENGINE_URL=http://127.0.0.1:8765
export TRANSUB_ENGINE_PYTHON="$PWD/.venv/bin/python"
setsid .venv/bin/python -m uvicorn transub_engine.server:create_app --factory --host 127.0.0.1 --port 8765 &
engine_pid=$!
web_pid=''
cleanup() {
    local groups=("-$engine_pid")
    if [[ -n "$web_pid" ]]; then groups+=("-$web_pid"); fi
    kill -TERM -- "${groups[@]}" 2>/dev/null || true
    for ((attempt=0; attempt<100; attempt++)); do
        local alive=0
        for group in "${groups[@]}"; do
            if kill -0 -- "$group" 2>/dev/null; then alive=1; break; fi
        done
        if (( alive == 0 )); then break; fi
        sleep 0.1
    done
    kill -KILL -- "${groups[@]}" 2>/dev/null || true
    wait || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT
setsid .venv/bin/python -m uvicorn app:app --app-dir web --host "${TRANSUB_WEB_HOST:-127.0.0.1}" --port 8766 --workers 1 --timeout-graceful-shutdown 3 &
web_pid=$!
wait -n "$engine_pid" "$web_pid"
