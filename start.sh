#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
    echo '请先按 README 创建 .venv 并安装 PySide6。' >&2
    exit 1
fi
qt_libs="$PWD/runtime/qt-libs/usr/lib/$(uname -m)-linux-gnu"
if [[ -d "$qt_libs" ]]; then
    export LD_LIBRARY_PATH="$qt_libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
exec .venv/bin/python main.py "$@"
