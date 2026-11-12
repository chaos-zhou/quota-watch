#!/bin/bash
# quota-watch 卸载脚本
#   ./uninstall.sh          保留 ~/.quota-watch（配置/日志）
#   ./uninstall.sh --purge  连同 ~/.quota-watch 一起删除
set -uo pipefail

QW_HOME="${QW_HOME:-$HOME/.quota-watch}"
LABEL="${QW_LABEL:-com.$(id -un | tr "[:upper:]" "[:lower:]").quota-watch}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

echo "停止并移除 launchd 任务"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl unload -w "$PLIST" 2>/dev/null || true
rm -f "$PLIST"

echo "从 ~/.zshrc 移除 PATH 段"
python3 - "$HOME/.zshrc" <<'PY'
import sys
path = sys.argv[1]
try:
    text = open(path, encoding="utf-8").read()
except OSError:
    sys.exit(0)
begin = "# >>> quota-watch claude shim >>>"
end = "# <<< quota-watch claude shim <<<"
if begin in text and end in text:
    head, _, rest = text.partition(begin)
    _, _, tail = rest.partition(end)
    open(path, "w", encoding="utf-8").write(head.rstrip("\n") + "\n" + tail.lstrip("\n"))
    print("  已移除")
else:
    print("  未找到该段，跳过")
PY

if [ "$PURGE" = "1" ]; then
  echo "删除 $QW_HOME"
  rm -rf "$QW_HOME"
else
  echo "保留 $QW_HOME（配置与日志）；如需删除请加 --purge"
fi
echo "完成。已打开的终端需重新打开才会恢复原始 claude 行为。"
