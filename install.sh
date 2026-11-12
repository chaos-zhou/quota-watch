#!/bin/bash
# quota-watch 安装脚本：把守护装到 ~/.quota-watch，注册 launchd 定时任务，
# 并把 claude 的 tmux 包装器加入 PATH。
#
#   ./install.sh           # 安装/更新（幂等）
#   ./install.sh --dry-run # 只打印将要做什么
#
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  cat >&2 <<'MSG'
quota-watch 的安装脚本只负责 macOS（launchd 常驻）。
Linux / WSL2 可以手动安装（详见 README「平台支持」）：

  mkdir -p ~/.quota-watch/bin
  install -m 0755 qw.py ~/.quota-watch/qw.py
  install -m 0755 bin/qw bin/claude ~/.quota-watch/bin/
  cp -n config.example.json ~/.quota-watch/config.json
  echo 'export PATH="$HOME/.quota-watch/bin:$PATH"' >> ~/.zshrc   # bash 用户改成 ~/.bashrc
  ~/.quota-watch/bin/qw --status
  # 常驻（二选一）：python3 ~/.quota-watch/qw.py --loop 30
  #                 或写一个 systemd user unit（README 里有模板）
MSG
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
QW_HOME="${QW_HOME:-$HOME/.quota-watch}"
LABEL="${QW_LABEL:-com.$(id -un | tr "[:upper:]" "[:lower:]").quota-watch}"
LA_DIR="$HOME/Library/LaunchAgents"
PLIST="$LA_DIR/$LABEL.plist"
UID_NUM="$(id -u)"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

say() { printf '%s\n' "$*"; }
doit() { if [ "$DRY" = "1" ]; then say "[dry-run] $*"; else eval "$@"; fi; }

say "quota-watch 安装"
say "  源目录 : $SRC"
say "  安装到 : $QW_HOME"
say "  plist  : $PLIST"

PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
  say "错误：找不到 python3" >&2
  exit 1
fi
TMUX_BIN="$(command -v tmux || true)"
if [ -z "$TMUX_BIN" ]; then
  say "警告：找不到 tmux，Claude 自动续跑将不可用（Codex 侧不受影响）" >&2
fi

doit "mkdir -p '$QW_HOME/bin' '$QW_HOME/logs' '$LA_DIR'"
doit "install -m 0755 '$SRC/qw.py' '$QW_HOME/qw.py'"
doit "install -m 0755 '$SRC/bin/claude' '$QW_HOME/bin/claude'"
doit "install -m 0755 '$SRC/bin/qw' '$QW_HOME/bin/qw'"
if [ -f "$QW_HOME/config.json" ]; then
  say "  配置已存在，保留：$QW_HOME/config.json"
else
  doit "install -m 0644 '$SRC/config.example.json' '$QW_HOME/config.json'"
fi

# --- PATH（幂等写入 ~/.zshrc） ---
MARK_BEGIN="# >>> quota-watch claude shim >>>"
MARK_END="# <<< quota-watch claude shim <<<"
if grep -qF "$MARK_BEGIN" "$HOME/.zshrc" 2>/dev/null; then
  say "  ~/.zshrc 已包含 quota-watch shim 段，跳过"
else
  if [ "$DRY" = "1" ]; then
    say "[dry-run] 向 ~/.zshrc 追加 PATH 段"
  else
    {
      printf '\n%s\n' "$MARK_BEGIN"
      printf '%s\n' '# 让交互式 claude 跑在 tmux（qa-*）里，额度恢复后 quota-watch 才能自动续跑。'
      printf '%s\n' '# 临时绕过：QW_NO_TMUX=1 claude'
      printf '%s\n' 'export PATH="$HOME/.quota-watch/bin:$PATH"'
      printf '%s\n' "$MARK_END"
    } >> "$HOME/.zshrc"
    say "  已写入 ~/.zshrc（新开终端或 source ~/.zshrc 生效）"
  fi
fi

# --- launchd plist ---
if [ "$DRY" = "1" ]; then
  say "[dry-run] 生成 $PLIST"
else
  sed -e "s|__PYTHON__|$PYTHON|g" \
      -e "s|__QW_HOME__|$QW_HOME|g" \
      -e "s|__LABEL__|$LABEL|g" \
      "$SRC/launchd/quota-watch.plist.template" > "$PLIST"
  say "  已生成 $PLIST"
fi

# --- 注册/重载 ---
if [ "$DRY" = "1" ]; then
  say "[dry-run] launchctl bootout/bootstrap $LABEL"
else
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  if ! launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null; then
    launchctl load -w "$PLIST" 2>/dev/null || say "  警告：launchctl 注册失败，请看下面的手动命令"
  fi
  launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  say "  已注册 launchd 任务（每 60 秒扫描一次）"
fi

if [ "$DRY" = "1" ]; then
  say "[dry-run] 完成"
  exit 0
fi

say ""
say "自检："
"$PYTHON" "$QW_HOME/qw.py" --once >/dev/null 2>&1 || true
"$PYTHON" "$QW_HOME/qw.py" --status || true
say ""
say "接下来："
say "  1) 新开一个终端（或 source ~/.zshrc），正常输入 claude 即可；它会在 qa-* 的 tmux 里跑"
say "  2) Codex 侧不需要改变使用方式，继续在桌面 App 里对话"
say "  3) 随时查看：~/.quota-watch/bin/qw --status     日志：~/.quota-watch/logs/qw.log"
say "  4) 先演练不真跑：~/.quota-watch/bin/qw --once --dry-run"
say "  5) 卸载：$SRC/uninstall.sh"
