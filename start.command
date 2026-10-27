#!/bin/bash
# macOS: chmod +x start.command, then double-click or run ./start.command.
set -u

pause_on_error() {
    if [[ -t 0 ]]; then
        printf '\n按回车关闭窗口…'
        IFS= read -r _whale_reply || true
    fi
}

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 1
cd -- "$script_dir" || exit 1

export PYTHONUTF8=1
export PYTHONUNBUFFERED=1

if [[ -n "${WHALE_PYTHON:-}" ]]; then
    candidates=("$WHALE_PYTHON")
else
    # Finder may not inherit Homebrew's PATH. Also support a local virtualenv.
    candidates=(
        "$script_dir/.venv/bin/python3"
        "$script_dir/.venv/bin/python"
        "python3"
        "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"
        "/opt/homebrew/bin/python3"
        "/usr/local/bin/python3"
        "python"
    )
fi

python_bin=""
for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys, tkinter, sqlite3; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        python_bin="$candidate"
        break
    fi
done

if [[ -z "$python_bin" ]]; then
    printf '%s\n' \
        '未找到带 Tkinter 支持的 Python 3.11 或更高版本。' \
        '建议安装 python.org 的 macOS Python 安装包，然后重新打开本脚本。' \
        '下载：https://www.python.org/downloads/macos/' \
        '如果已安装，可用 WHALE_PYTHON 指定 Python 可执行文件的完整路径。'
    pause_on_error
    exit 1
fi

if [[ ! -f .env && -z "${DS_BASE_URL:-${ds_base_url:-${base_url:-}}}" ]]; then
    printf '%s\n' \
        '缺少模型配置：请在本脚本所在目录创建 .env，可参考 .env.example。' \
        '填写 ds_base_url 和 api_key。模型在另一台电脑上时，请使用 Mac 能访问到的服务地址。'
    pause_on_error
    exit 1
fi

printf '%s\n' '正在打开鲸鱼娘的海边小屋…'
if "$python_bin" -m whale_friend "$@"; then
    exit 0
else
    status=$?
    printf '\n%s\n' '启动或运行未完成，请查看上方提示。'
    pause_on_error
    exit "$status"
fi
