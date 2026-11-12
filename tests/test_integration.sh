#!/bin/bash
# quota-watch 端到端演练（不联网、不消耗模型额度、不动系统目录）
#
#   bash tests/test_integration.sh
#
# 覆盖：
#   1) Codex：伪造 rollout（usage_limit_exceeded + resets_at）→ 检测 → 定位 thread
#   2) Codex：已经续跑过的会话不重复触发
#   3) Codex：到点后生成续跑命令（dry-run 只记录）
#   4) Claude：假 tmux 窗格出现 429 文案 → 检测
#   5) Claude：到点后真实调用 send-keys（写进假窗格记录，验证命令构造）
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
TMP="$(mktemp -d "$ROOT/tests/.it.XXXXXX")"
export QW_HOME="$TMP/home"
mkdir -p "$QW_HOME" "$TMP/sessions/2026/11/03" "$TMP/faketmux"

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

# ---------------------------------------------------------------- fixtures
python3 - "$TMP" "$HERE" <<'PY'
import json, os, sys, time
tmp, here = sys.argv[1], sys.argv[2]
now = int(time.time())
reset_at = now + 2 * 3600

def ev(payload, etype="event_msg"):
    return {"type": etype, "payload": payload}

def roll(path, events):
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")

def limited_rollout(thread_id, turn_id):
    return [
        ev({"id": thread_id}, etype="session_meta"),
        ev({"type": "task_started", "turn_id": turn_id}),
        ev({"type": "token_count", "info": {},
            "rate_limits": {"limit_id": "codex",
                            "primary": {"used_percent": 99.0, "window_minutes": 300, "resets_at": reset_at},
                            "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": reset_at + 86400}}}),
        ev({"type": "task_complete", "turn_id": turn_id,
            "error": {"message": "You’ve hit your usage limit. ... or try again at 2:33 PM.",
                      "codex_error_info": "usage_limit_exceeded"}}),
    ]

roll(os.path.join(tmp, "sessions/2026/11/03/rollout-2026-11-03T20-00-00-aaaaaaaa.jsonl"),
     limited_rollout("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "turn-1"))

# 白名单外的会话：应被忽略、不续跑
roll(os.path.join(tmp, "sessions/2026/11/03/rollout-2026-11-03T22-00-00-cccccccc.jsonl"),
     limited_rollout("cccccccc-dddd-eeee-ffff-aaaaaaaaaaaa", "turn-1"))

fake_cli = os.path.join(tmp, "fakebin", "codex")
os.makedirs(os.path.dirname(fake_cli), exist_ok=True)
with open(fake_cli, "w", encoding="utf-8") as fh:
    fh.write('#!/bin/sh\necho "Queued message fake for $*"\n')
os.chmod(fake_cli, 0o755)

resumed = limited_rollout("bbbbbbbb-cccc-dddd-eeee-ffffffffffff", "turn-1")
resumed.append(ev({"type": "task_started", "turn_id": "turn-2"}))
roll(os.path.join(tmp, "sessions/2026/11/03/rollout-2026-11-03T21-00-00-bbbbbbbb.jsonl"), resumed)

cfg = {
    "notify": False,
    "dry_run": False,
    "codex": {
        "enabled": True,
        "sessions_dir": os.path.join(tmp, "sessions"),
        "cli": fake_cli,
        "ensure_daemon": False,
        "auto_launch_app": False,
        "lookback_hours": 48,
        "whitelist_mode": "whitelist",
        "whitelist": ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"],
    },
    "claude": {
        "enabled": True,
        "tmux": os.path.join(here, "fake_tmux.sh"),
        "tmux_prefix": "qa-",
        "user_activity_grace_sec": 0,
    },
}
json.dump(cfg, open(os.path.join(tmp, "home", "config.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)

open(os.path.join(tmp, "faketmux/sessions"), "w").write("qa-it-test\n")
open(os.path.join(tmp, "faketmux/pane_qa-it-test"), "w").write(
    "⏺ 正在修改 service.py ...\n"
    "API Error: 429 {\"error\":{\"code\":\"1302\",\"message\":\"当前并发数过高，请稍后重试\"}}\n"
    "\n> \n"
)
print("fixtures ready")
PY

export FAKE_TMUX_DIR="$TMP/faketmux"
run_qw() { QW_HOME="$QW_HOME" FAKE_TMUX_DIR="$FAKE_TMUX_DIR" python3 "$ROOT/qw.py" "$@"; }

# -------------------------------------------------- 1) Codex 检测
echo "1) Codex 额度中断检测"
run_qw --once --dry-run >/dev/null 2>&1 || fail "qw --once 运行失败"
grep -q "检测到 Codex 额度中断" "$QW_HOME/logs/qw.log" || fail "未检测到 Codex 额度中断"
grep -q "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" "$QW_HOME/logs/qw.log" || fail "未定位到正确的 thread id"
pass "检测到 usage_limit_exceeded 并定位到 thread"

python3 - "$QW_HOME/state.json" <<'PY'
import json, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
keys = list(state["entries"])
assert "codex:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in keys, keys
assert not any("bbbbbbbb" in k for k in keys), f"已续跑的会话被误报: {keys}"
PY
pass "已续跑的会话未被误报"

# -------------------------------------------------- 2) Codex 到点续跑
echo "2) Codex 到点触发续跑（dry-run）"
python3 - "$QW_HOME/state.json" <<'PY'
import json, sys
path = sys.argv[1]
state = json.load(open(path, encoding="utf-8"))
for ent in state["entries"].values():
    ent["next_at"] = 0
json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
run_qw --once --dry-run >/dev/null 2>&1 || fail "第二轮运行失败"
grep -q "\[dry-run\] .*queue --thread" "$QW_HOME/logs/qw.log" || fail "未生成续跑命令"
grep -q "已触发 Codex 续跑" "$QW_HOME/logs/qw.log" || fail "未记录续跑动作"
pass "到点生成 codex queue --thread <id> --message … --resume"

# -------------------------------------------------- 3) Claude 检测
echo "3) Claude 限额文案检测"
run_qw --once --dry-run >/dev/null 2>&1 || fail "Claude 检测轮运行失败"
grep -q "检测到 Claude 额度中断" "$QW_HOME/logs/qw.log" || fail "未检测到 Claude 限额文案"
grep -q "qa-it-test" "$QW_HOME/logs/qw.log" || fail "未记录 Claude 会话名"
pass "从窗格识别到 429 并定位到 tmux 会话"

# -------------------------------------------------- 4) Claude 续跑
echo "4) Claude 到点 send-keys"
python3 - "$QW_HOME/state.json" <<'PY'
import json, sys
path = sys.argv[1]
state = json.load(open(path, encoding="utf-8"))
for ent in state["entries"].values():
    if ent.get("kind") == "claude":
        ent["next_at"] = 0
json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
run_qw --once >/dev/null 2>&1 || fail "Claude 续跑轮运行失败"
grep -q "已向 Claude 窗口发送" "$QW_HOME/logs/qw.log" || fail "未发送续跑按键"
grep -q "quota-watch 自动续跑" "$FAKE_TMUX_DIR/keys_qa-it-test" || fail "注入的续跑文本不正确"
pass "「继续」按预期构造并注入目标窗格"

# -------------------------------------------------- 5) 白名单
echo "5) 白名单之外不续跑"
grep -q "不在白名单，跳过自动续跑" "$QW_HOME/logs/qw.log" || fail "白名单外的会话未被打上 ignored"
python3 - "$QW_HOME/state.json" <<'PY'
import json, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
ent = state["entries"].get("codex:cccccccc-dddd-eeee-ffff-aaaaaaaaaaaa")
assert ent, list(state["entries"])
assert ent.get("status") == "ignored", ent
assert ent.get("attempts", 0) == 0, ent
PY
pass "白名单外的会话只记录 ignored，不排队、不重试"

echo ""
echo "全部通过 ✅"
echo "演练日志：$QW_HOME/logs/qw.log"
