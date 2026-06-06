# B2A-Studio 启动/退出共用（由 .command 脚本 source，勿直接双击）
b2a_port_pids() {
  lsof -ti "tcp:${B2A_PORT}" -sTCP:LISTEN 2>/dev/null || true
}

b2a_port_in_use() {
  [ -n "$(b2a_port_pids)" ]
}

b2a_port_free() {
  ! b2a_port_in_use
}

b2a_server_up() {
  curl -sf -o /dev/null --max-time 2 "${B2A_URL}" 2>/dev/null
}

b2a_stop_server() {
  local pids attempt
  if b2a_port_free; then
    return 0
  fi

  for attempt in 1 2; do
    pids=$(b2a_port_pids)
    [ -z "$pids" ] && return 0
    if [ "$attempt" -eq 1 ]; then
      echo "[$(date '+%F %T')] 停止旧进程: ${pids}" >>"${B2A_LOG}"
      kill $pids 2>/dev/null || true
    else
      echo "[$(date '+%F %T')] 强制结束: ${pids}" >>"${B2A_LOG}"
      kill -9 $pids 2>/dev/null || true
    fi
    for _ in $(seq 1 25); do
      b2a_port_free && return 0
      sleep 0.2
    done
  done

  echo "[$(date '+%F %T')] 错误: 端口 ${B2A_PORT} 仍被占用: $(b2a_port_pids)" >>"${B2A_LOG}"
  return 1
}

b2a_resolve_python() {
  if [ -n "${B2A_PYTHON:-}" ] && [ -x "${B2A_PYTHON}" ]; then
    echo "${B2A_PYTHON}"
    return 0
  fi
  if [ -x "/opt/anaconda3/bin/python" ]; then
    echo "/opt/anaconda3/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    echo "python3"
  fi
}

# 每次启动与 requirements.txt 对齐（含 streamlit 等 pinned 版本）
b2a_ensure_requirements() {
  local py req_file log_msg
  py="$(b2a_resolve_python)"
  req_file="${B2A_REQ_FILE:-requirements.txt}"
  if [ ! -f "$req_file" ]; then
    log_msg="[$(date '+%F %T')] 未找到依赖清单: ${req_file}"
    if [ -n "${B2A_LOG:-}" ]; then
      echo "$log_msg" >>"${B2A_LOG}"
    else
      echo "$log_msg" >&2
    fi
    osascript -e 'display alert "未找到 requirements.txt" message "请确认 B2A-Studio 目录完整。"' 2>/dev/null || true
    return 1
  fi
  echo "正在对齐 Python 依赖（requirements.txt），请稍候…"
  if ! "$py" -m pip install -r "$req_file" >>"${B2A_LOG:-/dev/null}" 2>&1; then
    osascript -e 'display alert "依赖安装失败" message "请在本机终端执行：\ncd B2A-Studio\npython3 -m pip install -r requirements.txt\n\n成功后重新双击打开。"' 2>/dev/null || true
    echo "[$(date '+%F %T')] requirements.txt 安装失败 (${py})" >>"${B2A_LOG:-/dev/null}"
    return 1
  fi
  if ! "$py" -c "import streamlit" 2>/dev/null; then
    osascript -e 'display alert "Streamlit 未就绪" message "依赖已执行安装但无法 import streamlit，请检查 Python 环境。"' 2>/dev/null || true
    return 1
  fi
  log_msg="[$(date '+%F %T')] requirements.txt 已对齐 (${py})"
  if [ -n "${B2A_LOG:-}" ]; then
    echo "$log_msg" >>"${B2A_LOG}"
  fi
  return 0
}

# MP3 内嵌 SYLT / 导出 LRC 依赖 mutagen（亦在 requirements.txt 中，此处作二次确认）
b2a_ensure_mutagen() {
  local py log_msg
  py="$(b2a_resolve_python)"
  if "$py" -c "import mutagen" 2>/dev/null; then
    log_msg="[$(date '+%F %T')] mutagen 已就绪 (${py})"
    if [ -n "${B2A_LOG:-}" ]; then
      echo "$log_msg" >>"${B2A_LOG}"
    fi
    return 0
  fi
  echo "正在安装 mutagen（有声书歌词内嵌 / LRC 导出）…"
  if ! "$py" -m pip install "mutagen==1.47.0" >>"${B2A_LOG:-/dev/null}" 2>&1; then
    osascript -e 'display alert "mutagen 安装失败" message "请在本机终端执行：python3 -m pip install mutagen\n\n安装成功后重新双击打开 B2A-Studio。"' 2>/dev/null || true
    echo "[$(date '+%F %T')] mutagen 安装失败 (${py})" >>"${B2A_LOG:-/dev/null}"
    return 1
  fi
  if "$py" -c "import mutagen" 2>/dev/null; then
    echo "[$(date '+%F %T')] mutagen 安装完成 (${py})" >>"${B2A_LOG:-/dev/null}"
    return 0
  fi
  osascript -e 'display alert "mutagen 安装后仍无法导入" message "请检查 Python 环境后重试。"' 2>/dev/null || true
  return 1
}

# 启动前轮转 launch 日志，避免 tee 累积数百 MB 拖慢磁盘与浏览器
b2a_rotate_launch_log() {
  local max_bytes=52428800
  if [ -z "${B2A_LOG:-}" ] || [ ! -f "${B2A_LOG}" ]; then
    return 0
  fi
  local size
  size=$(stat -f%z "${B2A_LOG}" 2>/dev/null || stat -c%s "${B2A_LOG}" 2>/dev/null || echo 0)
  if [ "${size:-0}" -le "${max_bytes}" ]; then
    return 0
  fi
  local rotated="${B2A_LOG}.1"
  rm -f "${rotated}"
  mv "${B2A_LOG}" "${rotated}"
  echo "[$(date '+%F %T')] 已轮转 launch 日志（原 ${size} 字节）→ ${rotated}" >>"${B2A_LOG}"
}

b2a_print_run_banner() {
  cat <<EOF

========================================
  B2A-Studio 正在本机运行
  网页：${B2A_URL}

  · 关掉浏览器标签 → 服务仍在（可再双击「打开」）
  · 关掉下面这个终端窗口 → 完全退出
  · 或在网页侧边栏点击「完全退出」
========================================

EOF
}
