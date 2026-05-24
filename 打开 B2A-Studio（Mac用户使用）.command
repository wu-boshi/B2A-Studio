#!/bin/bash
# 双击「打开 B2A-Studio（Mac用户使用）.command」启动；终端保持打开，关窗口即完全退出
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/B2A-Studio"
B2A_PORT=8501
B2A_URL="http://127.0.0.1:${B2A_PORT}/"
B2A_LOG="$APP/logs/streamlit.launch.log"
export B2A_PORT B2A_URL B2A_LOG

# shellcheck source=b2a-launch-lib.sh
source "$ROOT/b2a-launch-lib.sh"

if [ ! -d "$APP" ]; then
  osascript -e 'display alert "未找到 B2A-Studio 目录" message "请确认本脚本与 B2A-Studio 文件夹在同一目录下。"'
  exit 1
fi

cd "$APP"
mkdir -p logs

if command -v streamlit >/dev/null 2>&1; then
  STREAMLIT=(streamlit)
elif [ -x "/opt/anaconda3/bin/streamlit" ]; then
  STREAMLIT=(/opt/anaconda3/bin/streamlit)
  B2A_PYTHON="/opt/anaconda3/bin/python"
else
  STREAMLIT=(python3 -m streamlit)
fi
export B2A_PYTHON="${B2A_PYTHON:-$(b2a_resolve_python)}"

if ! b2a_ensure_mutagen; then
  echo "mutagen 未就绪，歌词功能可能不可用；详见 ${B2A_LOG}"
fi

prompt_when_running() {
  local msg="$1"
  osascript <<EOF
display dialog "${msg}" buttons {"取消", "重新启动", "打开网页"} default button "打开网页" with title "B2A-Studio"
return button returned of result
EOF
}

MSG_OK="B2A-Studio 已在运行（请查看是否还有一个终端窗口）。

· 「打开网页」：继续当前会话
· 「重新启动」：结束旧进程并重新打开
· 完全退出：关闭本终端窗口，或在网页侧边栏点击「完全退出」"

MSG_HUNG="检测到旧进程占用端口但网页无响应（可能已卡死）。

请选择「重新启动」。"

if b2a_port_in_use; then
  if b2a_server_up; then
    btn="$(prompt_when_running "$MSG_OK" || echo "取消")"
  else
    btn="$(prompt_when_running "$MSG_HUNG" || echo "取消")"
    if [ "$btn" = "打开网页" ]; then
      btn="重新启动"
    fi
  fi
  case "$btn" in
    打开网页)
      open "$B2A_URL"
      exit 0
      ;;
    重新启动)
      if ! b2a_stop_server; then
        osascript -e "display alert \"无法释放端口 ${B2A_PORT}\" message \"请关闭占用服务的终端窗口，或查看日志：${B2A_LOG}\""
        exit 1
      fi
      ;;
    *)
      exit 0
      ;;
  esac
fi

if ! b2a_port_free; then
  osascript -e "display alert \"端口 ${B2A_PORT} 仍被占用\" message \"请先关闭正在运行 B2A-Studio 的终端窗口，或选「重新启动」\""
  exit 1
fi

echo "[$(date '+%F %T')] 启动 Streamlit（前台）…" >>"$B2A_LOG"
b2a_print_run_banner

"${STREAMLIT[@]}" run app.py \
  --server.port "$B2A_PORT" \
  --server.headless true \
  --browser.gatherUsageStats false \
  2>&1 | tee -a "$B2A_LOG" &
SPID=$!

trap 'kill "$SPID" 2>/dev/null; wait "$SPID" 2>/dev/null' EXIT INT TERM

for _ in $(seq 1 30); do
  if b2a_server_up; then
    open "$B2A_URL"
    break
  fi
  sleep 1
done

if ! b2a_server_up; then
  kill "$SPID" 2>/dev/null || true
  wait "$SPID" 2>/dev/null || true
  osascript -e "display alert \"B2A-Studio 启动失败\" message \"请查看日志：${B2A_LOG}\""
  exit 1
fi

wait "$SPID"
