#!/bin/bash
# 假的 tmux —— 供集成演练使用，不启动真实 tmux server。
# 只实现 quota-watch 用到的四个动作：ls / capture-pane / display / send-keys。
# 状态目录由 FAKE_TMUX_DIR 指定。
set -euo pipefail
DIR="${FAKE_TMUX_DIR:?需要 FAKE_TMUX_DIR}"
mkdir -p "$DIR"

cmd="${1:-}"; shift || true
session=""; literal=""; fmt=""; txt=""
while [ $# -gt 0 ]; do
  case "$1" in
    -t) session="${2:-}"; shift 2 ;;
    -F) fmt="${2:-}"; shift 2 ;;
    -S) shift 2 ;;
    -p|-l) shift ;;
    --) shift ;;
    *) txt="$1"; shift ;;
  esac
done
session="${session#=}"

case "$cmd" in
  ls)
    if [ -f "$DIR/sessions" ]; then
      while IFS= read -r name; do
        [ -n "$name" ] && printf '%s\t%s\n' "$name" "${FAKE_TMUX_CREATED:-1700000000}"
      done < "$DIR/sessions"
    fi
    ;;
  capture-pane)
    cat "$DIR/pane_$session" 2>/dev/null || true
    ;;
  display)
    echo "${FAKE_TMUX_ACTIVITY:-0}"
    ;;
  send-keys)
    printf '%s\n' "$txt" >> "$DIR/keys_$session"
    ;;
  *)
    exit 1
    ;;
esac
