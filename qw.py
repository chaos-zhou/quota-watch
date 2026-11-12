#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quota-watch —— Codex / Claude Code 额度中断自动续跑守护。

两个数据源
==========
1) Codex 桌面 App（com.openai.codex）
   直接解析 ~/.codex/sessions/**/rollout-*.jsonl 的尾部事件，不读 sqlite，
   因此不会被正在写入的 App 锁住。

   * 额度打断一次 turn 时，rollout 末尾会出现：
       {"type":"event_msg","payload":{"type":"task_complete",
         "turn_id":"...","error":{"message":"You've hit your usage limit ...
         try again at 2:33 PM","codex_error_info":"usage_limit_exceeded"}}}
   * 同一次 turn 的 token_count 事件里带精确恢复时间（unix 秒）：
       payload.rate_limits.primary.resets_at   # 300 分钟窗口
       payload.rate_limits.secondary.resets_at # 10080 分钟窗口（周）
   * 恢复动作（写回桌面 App 里同一个会话）：
       codex queue --thread <uuid> --message "<继续>" --resume

2) Claude Code（官方或任意 API 中转都行：quota-watch 只看终端窗格里的报错文案）
   Claude 的会话 JSONL 不记录 API 错误，所以改为观察 tmux 窗格：
   安装的 claude 包装器（~/.quota-watch/bin/claude）会让交互式 claude 都跑在
   qa-* 的 tmux 会话里，本守护用 capture-pane 看到「429 / 额度 / rate limit」
   等文案后，等额度恢复再用 send-keys 把「继续」打回原来的窗口。

所有决策写入 ~/.quota-watch/logs/，状态存 ~/.quota-watch/state.json，可随时
`qw.py --status` 查看、`qw.py --reset` 清空。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
QW_HOME = os.environ.get("QW_HOME") or os.path.join(HOME, ".quota-watch")
def path_config() -> str:
    return os.path.join(QW_HOME, "config.json")


def path_state() -> str:
    return os.path.join(QW_HOME, "state.json")


def path_log_dir() -> str:
    return os.path.join(QW_HOME, "logs")


def path_log() -> str:
    return os.path.join(path_log_dir(), "qw.log")


def path_events() -> str:
    return os.path.join(path_log_dir(), "events.jsonl")


def path_lock() -> str:
    return os.path.join(QW_HOME, "qw.lock")

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
CODEX_CATALOG_DB = os.path.join(CODEX_HOME, "sqlite", "codex.db")
CLAUDE_HOME = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")

DEFAULT_CONFIG = {
    "notify": True,
    "dry_run": False,
    "retention_days": 14,
    "codex": {
        "enabled": True,
        "cli": "",  # 留空自动探测：PATH 里的 codex / ChatGPT.app 内置 CLI
        "app_bundle_id": "com.openai.codex",
        "sessions_dir": "~/.codex/sessions",
        "lookback_hours": 96,
        "tail_bytes": 262144,
        "resume_prompt": "【quota-watch 自动续跑】继续，从中断处接着做；不要重复已完成的部分，若已完成请直接说明。",
        "limit_error_infos": ["usage_limit_exceeded", "rate_limit_exceeded", "session_budget_exceeded"],
        "ensure_daemon": True,
        "daemon_start_timeout_sec": 90,
        "max_attempts": 5,
        "verify_window_sec": 900,
        "backoff_min": [15, 30, 60, 120],
        "default_reset_sec": 18000,
        "resume_mode": "queue",
        # all = 所有被打断的会话都自动续跑；whitelist = 只续跑 whitelist 里的 thread
        "whitelist_mode": "all",
        "whitelist": [],
        "dedupe_queued": True,
    },
    "claude": {
        "enabled": True,
        "tmux": "/opt/homebrew/bin/tmux",
        "tmux_prefix": "qa-",
        "resume_prompt": "【quota-watch 自动续跑】继续，从中断处接着做；不要重复已完成的部分，若已完成请直接说明。",
        "max_attempts": 8,
        "verify_window_sec": 900,
        "backoff_min": [10, 20, 40, 80, 160],
        "user_activity_grace_sec": 180,
        "pane_lines": 200,
        "tail_lines": 14,
        "default_reset_sec": 600,
    },
}

# ---------------------------------------------------------------- 基础工具


def now() -> float:
    return time.time()


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _ensure_dirs() -> None:
    os.makedirs(path_log_dir(), exist_ok=True)


def log(msg: str, level: str = "info", **fields) -> None:
    _ensure_dirs()
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    extra = (" " + json.dumps(fields, ensure_ascii=False)) if fields else ""
    line = f"{ts} [{level}] {msg}{extra}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(path_log(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        with open(path_events(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": ts, "level": level, "msg": msg, **fields}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def deep_merge(base: dict, override: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(path_config()):
        try:
            with open(path_config(), encoding="utf-8") as fh:
                cfg = deep_merge(cfg, json.load(fh))
        except (OSError, ValueError) as exc:
            log(f"读取配置失败，使用默认配置: {exc}", level="warn")
    return cfg


def load_state() -> dict:
    if not os.path.exists(path_state()):
        return {"version": 1, "entries": {}}
    try:
        with open(path_state(), encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("entries", {})
        return data
    except (OSError, ValueError) as exc:
        log(f"读取状态失败，重建: {exc}", level="warn")
        return {"version": 1, "entries": {}}


def save_config(cfg: dict) -> None:
    _ensure_dirs()
    tmp = path_config() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path_config())


def save_state(state: dict) -> None:
    _ensure_dirs()
    tmp = path_state() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path_state())


def prune_state(state: dict, cfg: dict) -> None:
    cutoff = now() - cfg.get("retention_days", 14) * 86400
    dead = []
    for key, ent in state["entries"].items():
        if ent.get("status") in ("done", "giveup") and (ent.get("first_seen") or 0) < cutoff:
            dead.append(key)
    for key in dead:
        state["entries"].pop(key, None)


def run(cmd: list[str], timeout: int = 60, extra_env: dict | None = None) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    env["QW_LAUNCHED"] = "1"
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except OSError as exc:
        return 126, "", str(exc)


def notify(cfg: dict, title: str, message: str) -> None:
    if not cfg.get("notify") or cfg.get("dry_run"):
        return
    if sys.platform == "darwin":
        # AppleScript 字符串转义
        def q(s: str) -> str:
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

        script = f"display notification {q(message)} with title {q('quota-watch')} subtitle {q(title)}"
        rc, _, err = run(["osascript", "-e", script], timeout=15)
        if rc != 0:
            log("通知发送失败", level="warn", err=err.strip()[:200])
        return
    # Linux 桌面通知（没装 notify-send 就静默跳过）
    if shutil.which("notify-send"):
        rc, _, err = run(["notify-send", title, message], timeout=15)
        if rc != 0:
            log("通知发送失败", level="warn", err=err.strip()[:200])


# ------------------------------------------------------- 恢复时间解析


_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

_AMP_RE = re.compile(r"(?:try again at|resets? at|available again at)\s+(\d{1,2}):(\d{2})\s*(AM|PM)", re.I)
_DATE_RE = re.compile(
    r"(?:try again at|resets? at)\s+([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.I,
)
_ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:?\d{2})?")
_IN_SEC_RE = re.compile(r"resets? in\s+(\d{1,3})\s*(s|sec|m|min|h|hr|hour)", re.I)


def _to_12h(hh: str, ap: str) -> int:
    h = int(hh) % 12
    return h + (12 if ap.upper() == "PM" else 0)


def parse_reset_time(text: str) -> float | None:
    """从错误/提示文案里解析恢复时间（本地时区）。解析不到返回 None。"""
    if not text:
        return None
    m = _DATE_RE.search(text)
    if m:
        mon, day, year, hh, mm, ap = m.groups()
        month = _MONTHS.get(mon[:3].title())
        if month:
            try:
                d = _dt.datetime(int(year), month, int(day), _to_12h(hh, ap), int(mm))
                return d.timestamp()
            except ValueError:
                pass
    m = _AMP_RE.search(text)
    if m:
        hh, mm, ap = m.groups()
        d = _dt.datetime.now().replace(hour=_to_12h(hh, ap), minute=int(mm), second=0, microsecond=0)
        if d.timestamp() <= now():
            d += _dt.timedelta(days=1)
        return d.timestamp()
    m = _ISO_RE.search(text)
    if m:
        date_s, hm, _sec, tz = m.groups()
        try:
            d = _dt.datetime.strptime(f"{date_s} {hm}", "%Y-%m-%d %H:%M")
            if tz and tz != "Z":
                sign = 1 if tz[0] == "+" else -1
                hh = int(tz[1:3])
                mm2 = int(tz[-2:])
                d -= _dt.timedelta(minutes=sign * (hh * 60 + mm2))
            return d.timestamp()
        except ValueError:
            pass
    m = _IN_SEC_RE.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        mult = 1 if unit.startswith("s") else 60 if unit.startswith("m") else 3600
        return now() + n * mult
    return None


# ------------------------------------------------------------ Codex 侧


def codex_sessions_dir(cfg: dict) -> str:
    return os.path.expanduser(cfg["codex"].get("sessions_dir") or os.path.join(CODEX_HOME, "sessions"))


def iter_rollouts(sessions_dir: str, lookback_hours: float):
    root = os.path.expanduser(sessions_dir)
    if not os.path.isdir(root):
        return
    cutoff = now() - lookback_hours * 3600
    for dirpath, _dirnames, files in os.walk(root):
        for name in files:
            if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_size == 0 or st.st_mtime < cutoff:
                continue
            yield path, st.st_mtime


def read_tail_events(path: str, max_bytes: int) -> list[dict]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
                raw = fh.read()
                nl = raw.find(b"\n")
                raw = raw[nl + 1:] if nl >= 0 else raw
            else:
                raw = fh.read()
    except OSError:
        return []
    events: list[dict] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def analyze_codex_events(events: list[dict]) -> dict:
    thread_id = None
    terminal = None
    rate_limits = None
    for ev in events:
        etype = ev.get("type")
        payload = ev.get("payload") or {}
        if etype == "session_meta":
            thread_id = payload.get("id") or payload.get("thread_id") or thread_id
            continue
        if etype != "event_msg":
            continue
        ptype = payload.get("type")
        if ptype == "task_started":
            terminal = ("started", payload)
        elif ptype == "task_complete":
            terminal = ("complete", payload)
        elif ptype == "turn_aborted":
            terminal = ("aborted", payload)
        elif ptype == "token_count" and payload.get("rate_limits"):
            rate_limits = payload["rate_limits"]
    return {"thread_id": thread_id, "terminal": terminal, "rate_limits": rate_limits}


def reset_from_rate_limits(rate_limits: dict | None) -> float | None:
    if not rate_limits:
        return None
    hot: list[float] = []
    fallback = None
    for key in ("primary", "secondary"):
        window = rate_limits.get(key) or {}
        resets_at = window.get("resets_at")
        used = window.get("used_percent")
        if isinstance(resets_at, (int, float)) and resets_at > now() - 120:
            if fallback is None:
                fallback = float(resets_at)
            if isinstance(used, (int, float)) and used >= 95.0:
                hot.append(float(resets_at))
    if hot:
        return min(hot)
    return fallback


def codex_limited(analysis: dict, limit_infos: set[str]) -> dict | None:
    terminal = analysis.get("terminal")
    if not terminal or terminal[0] != "complete":
        return None
    payload = terminal[1] or {}
    err = payload.get("error")
    if not err:
        return None
    info = (err.get("codex_error_info") or "").lower()
    message = err.get("message") or ""
    if info in limit_infos or re.search(r"(usage limit|rate limit|额度|try again at)", message, re.I):
        return {"turn_id": payload.get("turn_id"), "error_info": info, "message": message}
    return None


def _socket_alive(path: str) -> bool:
    if not os.path.exists(path):
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1.5)
    try:
        sock.connect(path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def managed_daemon_socket() -> str:
    return os.path.join(CODEX_HOME, "app-server-control", "app-server-control.sock")


def daemon_alive() -> bool:
    """CLI 的本地 app-server daemon —— `codex queue --resume` 真正依赖的是它。"""
    return _socket_alive(managed_daemon_socket())


def desktop_app_socket_alive() -> bool:
    """桌面 App 自己的 app-server（对话 UI 用；queue 不走它）。"""
    return _socket_alive(os.path.join(CODEX_HOME, "ipc", "ipc.sock"))


def queue_pending_age(thread_id: str) -> float | None:
    """该 thread 最早一条排队消息已等待多少秒；没有排队消息返回 None。"""
    path = os.path.join(CODEX_HOME, "queue_1.sqlite")
    if not os.path.exists(path):
        return None
    for uri in (f"file:{path}?mode=ro", f"file:{path}?immutable=1"):
        try:
            con = sqlite3.connect(uri, uri=True, timeout=2.5)
            try:
                row = con.execute(
                    "select min(created_at_ms), count(*) from queued_items where thread_id = ?",
                    (thread_id,),
                ).fetchone()
            finally:
                con.close()
            if not row or not row[1]:
                return None
            return max(0.0, (now() * 1000 - float(row[0])) / 1000.0)
        except (sqlite3.Error, TypeError, ValueError):
            continue
    return None


def resolve_codex_cli(codex_cfg: dict) -> str | None:
    """定位 codex CLI：配置 → PATH → ChatGPT.app 内置。"""
    configured = os.path.expanduser(str(codex_cfg.get("cli") or "").strip())
    if configured and os.path.exists(configured):
        return configured
    found = shutil.which("codex")
    if found:
        return found
    bundled = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
    if os.path.exists(bundled):
        return bundled
    return configured or None


def ensure_codex_daemon(cfg: dict) -> tuple[bool, str]:
    codex_cfg = cfg["codex"]
    if daemon_alive():
        return True, "already-running"
    cli = resolve_codex_cli(codex_cfg)
    if not cli or not os.path.exists(cli):
        return False, f"未找到 codex CLI（配置 codex.cli 或把 codex 放进 PATH）: {codex_cfg.get('cli') or ''}"
    timeout = int(codex_cfg.get("daemon_start_timeout_sec", 90)) + 60
    rc, out, err = run([cli, "app-server", "daemon", "start"], timeout=timeout)
    detail = ((out or "").strip() or (err or "").strip())[-400:]
    if daemon_alive():
        log("已启动 Codex 本地 app-server daemon")
        return True, "started: " + detail
    return False, f"daemon 启动失败(exit={rc}): {detail}"


def codex_resume(cfg: dict, thread_id: str, prompt: str, dry: bool) -> tuple[bool, str]:
    codex_cfg = cfg["codex"]
    cli = resolve_codex_cli(codex_cfg)
    if not cli or not os.path.exists(cli):
        return False, f"未找到 codex CLI（配置 codex.cli 或把 codex 放进 PATH）: {codex_cfg.get('cli') or ''}"
    mode = codex_cfg.get("resume_mode", "queue")

    if mode == "exec":
        cmd = [cli, "exec", "resume", thread_id, prompt, "--json"]
        if dry:
            log("[dry-run] " + " ".join(cmd))
            return True, "dry-run"
        rc, out, err = run(cmd, timeout=1800)
        detail = ((out or "").strip() or (err or "").strip())[-400:]
        return (rc == 0), (detail or f"exit={rc}")

    # queue 模式：需要 CLI 的本地 daemon；daemon 起来后队列会被自动执行
    if dry:
        log("[dry-run] ensure daemon + " + " ".join(
            [cli, "queue", "--thread", thread_id, "--message", "<resume_prompt>", "--resume"]))
        return True, "dry-run"
    if codex_cfg.get("ensure_daemon", True):
        ok, detail = ensure_codex_daemon(cfg)
        if not ok:
            return False, detail
    cmd = [cli, "queue", "--thread", thread_id, "--message", prompt, "--resume"]
    rc, out, err = run(cmd, timeout=180)
    detail = ((out or "").strip() or (err or "").strip())[:400]
    if rc != 0:
        return False, f"exit={rc} {detail}"
    if re.search(r"could not resume|failed to resume|resume failed", out or "", re.I):
        return True, "已排队但本次续跑失败(daemon 启动后会执行): " + detail
    return True, detail or "ok"


def _backoff(cfg: dict, attempts: int) -> float:
    table = cfg.get("backoff_min") or [15, 30]
    minutes = table[min(max(attempts, 1) - 1, len(table) - 1)]
    return float(minutes) * 60


def handle_codex_limited(cfg: dict, key: str, ent: dict, analysis: dict, dry: bool) -> None:
    """ent 已存在且仍是同一个被限额 turn：判断是否该续跑。"""
    codex_cfg = cfg["codex"]
    if now() < float(ent.get("next_at") or 0):
        return
    if ent.get("attempts", 0) >= int(codex_cfg.get("max_attempts", 5)):
        if ent.get("status") != "giveup":
            ent["status"] = "giveup"
            log("Codex 续跑次数用尽，停止", level="warn", thread=ent["id"], attempts=ent.get("attempts"))
            notify(cfg, "Codex 续跑放弃", f"会话 {ent['id'][:8]} 重试 {ent.get('attempts')} 次仍未恢复")
        return
    verify = float(codex_cfg.get("verify_window_sec", 900))
    pending_age = queue_pending_age(ent["id"]) if codex_cfg.get("dedupe_queued", True) else None
    if pending_age is not None and pending_age < verify:
        ent["next_at"] = now() + verify
        log("Codex 已有排队消息，等待 daemon 执行（不重复排队）",
            thread=ent["id"], turn=ent.get("turn"), pending_sec=round(pending_age))
        return
    if pending_age is not None:
        log("Codex 排队消息迟迟未执行，重新排队", level="warn",
            thread=ent["id"], pending_sec=round(pending_age))

    ok, detail = codex_resume(cfg, ent["id"], codex_cfg["resume_prompt"], dry)
    ent["attempts"] = int(ent.get("attempts", 0)) + 1
    ent["last_detail"] = detail
    if ok:
        ent["status"] = "resuming"
        ent["next_at"] = now() + float(codex_cfg.get("verify_window_sec", 900))
        log("已触发 Codex 续跑", thread=ent["id"], turn=ent.get("turn"),
            attempt=ent["attempts"], detail=detail[:200])
        notify(cfg, "Codex 已自动续跑", f"会话 {ent['id'][:8]} 第 {ent['attempts']} 次续跑已发出")
    else:
        ent["status"] = "waiting"
        ent["next_at"] = now() + _backoff(codex_cfg, ent["attempts"])
        log("Codex 续跑失败", level="warn", thread=ent["id"], attempt=ent["attempts"],
            next_try=fmt_ts(ent["next_at"]), detail=detail[:300])


def codex_whitelist_mode(codex_cfg: dict) -> str:
    return str(codex_cfg.get("whitelist_mode") or "all").lower()


def codex_whitelist(codex_cfg: dict) -> list[str]:
    return [str(x).strip().lower() for x in (codex_cfg.get("whitelist") or []) if str(x).strip()]


def codex_thread_allowed(codex_cfg: dict, thread_id: str) -> bool:
    if codex_whitelist_mode(codex_cfg) != "whitelist":
        return True
    return (thread_id or "").lower() in codex_whitelist(codex_cfg)


def all_codex_thread_ids(cfg: dict) -> list[str]:
    ids = set()
    for key in (load_state().get("entries") or {}):
        if key.startswith("codex:"):
            ids.add(key.split(":", 1)[1])
    codex_cfg = cfg["codex"]
    for path, _mtime in iter_rollouts(codex_sessions_dir(cfg), float(codex_cfg.get("lookback_hours", 96))):
        if is_subagent_rollout(path):
            continue
        tid = thread_id_from_rollout_name(path)
        if tid:
            ids.add(tid)
    return sorted(ids)


class AmbiguousThreadError(Exception):
    """前缀命中了多个会话，需要用更长的前缀或完整 UUID。"""

    def __init__(self, ref: str, candidates: list[str]):
        super().__init__(ref)
        self.ref = ref
        self.candidates = candidates


def resolve_thread_id(cfg: dict, ref: str) -> str | None:
    """把 UUID 前缀或 last 解析成完整 thread id。"""
    ref = (ref or "").strip().lower()
    if not ref:
        return None
    codex_cfg = cfg["codex"]
    if ref in ("last", "latest"):
        limit_infos = {s.lower() for s in codex_cfg.get("limit_error_infos", [])}
        rollouts = sorted(
            iter_rollouts(codex_sessions_dir(cfg), float(codex_cfg.get("lookback_hours", 96))),
            key=lambda x: x[1],
            reverse=True,
        )
        for path, _mtime in rollouts:
            if is_subagent_rollout(path):
                continue
            analysis = analyze_codex_events(read_tail_events(path, int(codex_cfg.get("tail_bytes", 262144))))
            tid = analysis.get("thread_id") or thread_id_from_rollout_name(path)
            if tid and codex_limited(analysis, limit_infos):
                return tid
        return None
    if ref.startswith("#"):
        try:
            idx = int(ref[1:])
        except ValueError:
            return None
        rows = collect_codex_sessions(cfg, max(idx, 1))
        if 1 <= idx <= len(rows):
            return rows[idx - 1]["id"]
        return None
    ids = all_codex_thread_ids(cfg)
    if ref in ids:
        return ref
    matched = [i for i in ids if i.startswith(ref)]
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        raise AmbiguousThreadError(ref, matched)
    catalog = load_thread_catalog()
    hits = [
        tid for tid, info in catalog.items()
        if ref in (info.get("title") or "").lower() or ref in (info.get("cwd") or "").lower()
    ]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise AmbiguousThreadError(ref, sorted(hits))
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", ref):
        return ref
    return None


def print_whitelist(cfg: dict) -> None:
    codex_cfg = cfg.get("codex") or {}
    mode = codex_whitelist_mode(codex_cfg)
    wl = [str(x) for x in (codex_cfg.get("whitelist") or [])]
    print(f"Codex 续跑范围：{mode}")
    if mode != "whitelist":
        print("  all = 所有被打断的会话都会自动续跑（切换：qw --mode whitelist）")
    if not wl:
        print("  白名单为空（whitelist 模式下不会自动续跑任何会话）")
    catalog = load_thread_catalog()
    for tid in wl:
        title = (catalog.get(tid) or {}).get("title") or ""
        print(f"  - {tid}" + (f"  ({title})" if title else ""))


_ROLLOUT_THREAD_RE = re.compile(
    r"rollout-[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9T-]+-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


def thread_id_from_rollout_name(path: str) -> str | None:
    """会话很长时 tail 里读不到 session_meta，用文件名里的 UUID 兜底。"""
    m = _ROLLOUT_THREAD_RE.search(os.path.basename(path))
    return m.group(1) if m else None


def read_head_meta(path: str) -> dict:
    """读 rollout 第一行的 session_meta payload（开销很小）。"""
    try:
        with open(path, "rb") as fh:
            first = fh.readline(1 << 16)
        meta = json.loads(first)
        return meta.get("payload") or {}
    except (OSError, ValueError):
        return {}


def is_subagent_rollout(path: str) -> bool:
    """子代理 fork 出来的会话由父会话推进，守护不应单独恢复它们。"""
    source = read_head_meta(path).get("source")
    return isinstance(source, dict) and bool(source.get("subagent"))


def load_thread_catalog() -> dict[str, dict]:
    """读 Codex 的本地会话目录库（只读），拿到标题/目录，方便人认出 UUID。"""
    if not os.path.exists(CODEX_CATALOG_DB):
        return {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CODEX_CATALOG_DB, uri=True)
        try:
            rows = con.execute(
                "SELECT thread_id, display_title, cwd, source_updated_at FROM local_thread_catalog "
                "WHERE missing_candidate = 0"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {
        str(r[0]): {"title": r[1] or "", "cwd": r[2] or "", "updated_at": r[3] or 0}
        for r in rows
    }


def collect_codex_sessions(cfg: dict, limit: int = 15) -> list[dict]:
    codex_cfg = cfg["codex"]
    limit_infos = {s.lower() for s in codex_cfg.get("limit_error_infos", [])}
    catalog = load_thread_catalog()
    rows = []
    for path, mtime in iter_rollouts(codex_sessions_dir(cfg), float(codex_cfg.get("lookback_hours", 96))):
        if is_subagent_rollout(path):
            continue
        tid = thread_id_from_rollout_name(path)
        if not tid:
            continue
        analysis = analyze_codex_events(read_tail_events(path, int(codex_cfg.get("tail_bytes", 262144))))
        info = catalog.get(tid) or {}
        meta = read_head_meta(path)
        rows.append({
            "id": tid,
            "mtime": mtime,
            "title": info.get("title") or "",
            "cwd": info.get("cwd") or (meta.get("cwd") or ""),
            "limited": bool(codex_limited(analysis, limit_infos)),
            "rollout": path,
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


def print_sessions(cfg: dict, limit: int = 15) -> None:
    rows = collect_codex_sessions(cfg, limit)
    state = load_state()
    wl = set(codex_whitelist(cfg["codex"]))
    print(f"最近 {len(rows)} 个 Codex 会话（按最后活动时间排序）：")
    max_age = float((cfg.get("codex") or {}).get("max_interrupt_age_hours", 24)) * 3600
    for i, r in enumerate(rows, 1):
        flags = []
        if r["limited"]:
            if now() - r["mtime"] > max_age:
                flags.append("已限流(超过%d小时,不会续跑)" % round(max_age / 3600))
            else:
                flags.append("已限流")
        if r["id"].lower() in wl:
            flags.append("白名单")
        ent = (state.get("entries") or {}).get("codex:" + r["id"])
        if ent:
            if ent.get("status") in ("waiting", "resuming"):
                flags.append("待续跑@" + fmt_ts(ent.get("next_at")))
            elif ent.get("status") == "ignored":
                flags.append("已忽略(非白名单)")
        flag_s = ("[" + "/".join(flags) + "]") if flags else ""
        print(f" {i:2d}) {r['id'][:8]}…  {fmt_ts(r['mtime'])}  {flag_s}")
        print(f"     标题: {r['title'] or '（无标题）'}")
        print(f"     目录: {r['cwd'] or '-'}")
    print()
    print("加入白名单：qw --watch #编号 | qw --watch <8位ID或更长> | qw --watch <标题关键字> | qw --watch last")



def scan_codex(cfg: dict, state: dict, dry: bool) -> None:
    codex_cfg = cfg["codex"]
    if not codex_cfg.get("enabled", True):
        return
    limit_infos = {s.lower() for s in codex_cfg.get("limit_error_infos", [])}
    seen_threads: set[str] = set()
    max_age = float(codex_cfg.get("max_interrupt_age_hours", 24)) * 3600
    for path, mtime in iter_rollouts(codex_sessions_dir(cfg), float(codex_cfg.get("lookback_hours", 96))):
        if is_subagent_rollout(path):
            continue
        # 早就中断、之后没再写的旧会话不复活，避免隔天突然开一个无关 turn
        if now() - mtime > max_age:
            continue
        analysis = analyze_codex_events(read_tail_events(path, int(codex_cfg.get("tail_bytes", 262144))))
        thread_id = analysis.get("thread_id") or thread_id_from_rollout_name(path)
        if not thread_id:
            continue
        seen_threads.add(thread_id)
        key = "codex:" + thread_id
        ent = state["entries"].get(key)
        limited = codex_limited(analysis, limit_infos)
        if not limited:
            if ent and ent.get("status") in ("waiting", "resuming"):
                log("Codex 会话已恢复正常，清理状态", thread=thread_id)
                ent["status"] = "done"
                ent["resolved_at"] = now()
            continue
        turn = limited.get("turn_id") or "unknown"
        if not codex_thread_allowed(codex_cfg, thread_id):
            # 不在白名单：只留一条 ignored 记录方便排查，不排队、不续跑
            if not ent or ent.get("status") != "ignored":
                state["entries"][key] = {
                    "kind": "codex",
                    "id": thread_id,
                    "turn": turn,
                    "rollout": path,
                    "first_seen": now(),
                    "reset_at": None,
                    "attempts": 0,
                    "next_at": 0,
                    "status": "ignored",
                    "reason": "not in whitelist",
                    "message": (limited.get("message") or "")[:400],
                }
                log("Codex 会话不在白名单，跳过自动续跑", thread=thread_id, turn=turn)
            continue
        if ent and ent.get("status") == "ignored":
            ent = None  # 刚被加入白名单，按新中断重新登记
        if ent and ent.get("turn") == turn:
            handle_codex_limited(cfg, key, ent, analysis, dry)
            continue
        # 新的被限额 turn
        reset_at = (
            parse_reset_time(limited.get("message", ""))
            or reset_from_rate_limits(analysis.get("rate_limits"))
            or now() + float(codex_cfg.get("default_reset_sec", 18000))
        )
        first_at = max(reset_at + 60, now() + 30)
        ent = {
            "kind": "codex",
            "id": thread_id,
            "turn": turn,
            "rollout": path,
            "first_seen": now(),
            "reset_at": reset_at,
            "attempts": 0,
            "next_at": first_at,
            "status": "waiting",
            "error_info": limited.get("error_info"),
            "message": (limited.get("message") or "")[:400],
        }
        state["entries"][key] = ent
        log("检测到 Codex 额度中断", thread=thread_id, turn=turn,
            error_info=ent["error_info"], reset_at=fmt_ts(reset_at),
            detail=ent["message"][:180])
        notify(cfg, "Codex 额度中断", f"会话 {thread_id[:8]}，{fmt_ts(reset_at)} 自动续跑")
    # 最近完全没出现的线程保持状态不动（可能被归档）


# ----------------------------------------------------------- Claude 侧

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")

_PANIC_PATTERNS = [re.compile(p, re.I) for p in (
    r"API Error:\s*(?:429|overloaded)",
    r"\b429\b[^\n]{0,80}(?:rate|limit|quota|too many)",
    r"rate_limit_error",
    r"rate.?limit(?:ed)?\b",
    r"usage limit",
    r"You've hit your (?:session|weekly|Opus|Sonnet) limit",
    r"(?:session|weekly) limit reached",
    r"Too Many Requests",
    r"当前并发数",
    r"(?:额度|限额|配额)[^\n]{0,24}(?:已用尽|用完|超出|超限|不足)",
    r"(?:已用尽|超出|超限)[^\n]{0,20}(?:额度|限额|配额)",
    r"请求过于频繁",
)]

_BUSY_PATTERNS = [re.compile(p, re.I) for p in (
    r"Retrying in \d+",
    r"esc to interrupt",
    r"to interrupt",
    r"^\s*[✻✽✳·⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+\S+…?\s*$",
    r"Thinking…",
)]


def tmux_bin(cfg: dict) -> str:
    cand = cfg["claude"].get("tmux") or "tmux"
    if os.path.exists(cand):
        return cand
    for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
        if os.path.exists(p):
            return p
    return cand


def tmux_sessions(cfg: dict) -> list[dict]:
    tmux = tmux_bin(cfg)
    rc, out, err = run([tmux, "ls", "-F", "#{session_name}\t#{session_created}"], timeout=15)
    if rc != 0:
        return []
    prefix = cfg["claude"].get("tmux_prefix", "")
    items = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, created = parts[0], parts[1]
        if prefix and not name.startswith(prefix):
            continue
        items.append({"name": name, "created": int(created) if created.isdigit() else 0})
    return items


def tmux_capture(cfg: dict, session: str, lines: int) -> str:
    tmux = tmux_bin(cfg)
    rc, out, err = run([tmux, "capture-pane", "-p", "-t", session, "-S", f"-{lines}"], timeout=20)
    if rc != 0:
        return ""
    return _ANSI_RE.sub("", out)


def tmux_user_activity(cfg: dict, session: str) -> float:
    tmux = tmux_bin(cfg)
    rc, out, _err = run([tmux, "display", "-p", "-t", session, "#{session_activity}"], timeout=10)
    try:
        return float(out.strip()) if rc == 0 and out.strip() else 0.0
    except ValueError:
        return 0.0


def claude_pane_limited(cfg: dict, pane_text: str) -> tuple[bool, str, float | None]:
    lines = [ln.rstrip() for ln in pane_text.splitlines() if ln.strip()]
    tail_n = int(cfg["claude"].get("tail_lines", 14))
    tail = lines[-tail_n:]
    joined = "\n".join(tail)
    busy = any(p.search(joined) for p in _BUSY_PATTERNS)
    if busy:
        return False, "", None
    hit = next((p.pattern for p in _PANIC_PATTERNS if p.search(joined)), "")
    if not hit:
        return False, "", None
    reset = parse_reset_time(joined)
    return True, hit, reset


def claude_send_continue(cfg: dict, session: str, prompt: str, dry: bool) -> tuple[bool, str]:
    tmux = tmux_bin(cfg)
    if dry:
        log("[dry-run] tmux send-keys -t %s -l -- %r; Enter" % (session, prompt))
        return True, "dry-run"
    rc, out, err = run([tmux, "send-keys", "-t", session, "-l", "--", prompt], timeout=20)
    if rc != 0:
        return False, f"send-keys 失败: {err.strip()[:200]}"
    time.sleep(0.6)
    rc2, out2, err2 = run([tmux, "send-keys", "-t", session, "Enter"], timeout=20)
    if rc2 != 0:
        return False, f"Enter 失败: {err2.strip()[:200]}"
    return True, "sent"


def scan_claude(cfg: dict, state: dict, dry: bool) -> None:
    claude_cfg = cfg["claude"]
    if not claude_cfg.get("enabled", True):
        return
    sessions = tmux_sessions(cfg)
    alive = {s["name"] for s in sessions}
    for name, ent in list(state["entries"].items()):
        if ent.get("kind") == "claude" and ent.get("id") not in alive and ent.get("status") in ("waiting", "resuming"):
            ent["status"] = "done"
            ent["resolved_at"] = now()
            log("Claude tmux 会话已退出，清理状态", session=ent.get("id"))

    for sess in sessions:
        name = sess["name"]
        pane = tmux_capture(cfg, name, int(claude_cfg.get("pane_lines", 200)))
        if not pane:
            continue
        limited, pattern, reset_at = claude_pane_limited(cfg, pane)
        key = "claude:" + name
        ent = state["entries"].get(key)
        if not limited:
            if ent and ent.get("status") in ("waiting", "resuming"):
                ent["status"] = "done"
                ent["resolved_at"] = now()
                log("Claude 会话已恢复，清理状态", session=name)
            continue
        if not ent or ent.get("status") in ("done", "giveup"):
            reset_at = reset_at or (now() + float(claude_cfg.get("default_reset_sec", 600)))
            ent = {
                "kind": "claude",
                "id": name,
                "pattern": pattern,
                "first_seen": now(),
                "reset_at": reset_at,
                "attempts": 0,
                "next_at": max(reset_at + 30, now() + 30),
                "status": "waiting",
            }
            state["entries"][key] = ent
            log("检测到 Claude 额度中断", session=name, pattern=pattern, reset_at=fmt_ts(reset_at))
            notify(cfg, "Claude 额度中断", f"{name}，{fmt_ts(reset_at)} 自动续跑")
            continue
        if now() < float(ent.get("next_at") or 0):
            continue
        if ent.get("attempts", 0) >= int(claude_cfg.get("max_attempts", 8)):
            ent["status"] = "giveup"
            log("Claude 续跑次数用尽，停止", level="warn", session=name, attempts=ent.get("attempts"))
            notify(cfg, "Claude 续跑放弃", f"{name} 重试 {ent.get('attempts')} 次仍未恢复")
            continue
        last_activity = tmux_user_activity(cfg, name)
        grace = float(claude_cfg.get("user_activity_grace_sec", 180))
        if last_activity and (now() - last_activity) < grace:
            log("用户刚有输入，本轮跳过", session=name,
                last_activity=fmt_ts(last_activity))
            continue
        ok, detail = claude_send_continue(cfg, name, claude_cfg["resume_prompt"], dry)
        ent["attempts"] = int(ent.get("attempts", 0)) + 1
        ent["last_detail"] = detail
        if ok:
            ent["status"] = "resuming"
            ent["next_at"] = now() + float(claude_cfg.get("verify_window_sec", 900))
            log("已向 Claude 窗口发送「继续」", session=name, attempt=ent["attempts"])
            notify(cfg, "Claude 已自动续跑", f"{name} 第 {ent['attempts']} 次续跑已发送")
        else:
            ent["status"] = "waiting"
            ent["next_at"] = now() + _backoff(claude_cfg, ent["attempts"])
            log("Claude 续跑失败", level="warn", session=name, detail=detail[:200])


# --------------------------------------------------------------- 主流程


class QwLock:
    def __init__(self, path: str):
        self.path = path
        self.fh = None

    def __enter__(self):
        _ensure_dirs()
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.fh.close()
            self.fh = None
            return False

    def __exit__(self, *exc):
        if self.fh:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            finally:
                self.fh.close()


def print_status(cfg: dict, state: dict) -> None:
    entries = state.get("entries") or {}
    print(f"quota-watch 状态：{len(entries)} 条记录  (config: {path_config()})")
    if not entries:
        print("  （空）")
    for key, ent in sorted(entries.items(), key=lambda kv: kv[1].get("first_seen", 0)):
        print(
            f"  [{ent.get('status','?'):9s}] {key}\n"
            f"      turn={ent.get('turn') or '-'}  尝试={ent.get('attempts',0)}"
            f"  首次={fmt_ts(ent.get('first_seen'))}  恢复时间={fmt_ts(ent.get('reset_at'))}"
            f"  下次动作={fmt_ts(ent.get('next_at'))}\n"
            f"      msg={(ent.get('message') or ent.get('pattern') or '')[:120]}"
        )
    print_whitelist(cfg)
    print(f"上次扫描: {fmt_ts(state.get('last_scan'))}"
          f"  (dry_run={state.get('last_scan_dry', False)})")
    print()
    print(f"Codex 本地 daemon(queue 依赖): {'运行中' if daemon_alive() else '未运行'}")
    print(f"Codex 桌面 App server: {'运行中' if desktop_app_socket_alive() else '未运行'}")
    print(f"tmux 会话: {[s['name'] for s in tmux_sessions(cfg)] or '（无）'}")


def run_once(cfg: dict, dry: bool) -> int:
    state = load_state()
    prune_state(state, cfg)
    scan_codex(cfg, state, dry)
    scan_claude(cfg, state, dry)
    state["last_scan"] = now()
    state["last_scan_dry"] = bool(dry)
    save_state(state)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="quota-watch：额度中断自动续跑")
    parser.add_argument("--once", action="store_true", help="扫描一次后退出（launchd 用）")
    parser.add_argument("--loop", type=int, metavar="SEC", help="循环运行，间隔 SEC 秒")
    parser.add_argument("--dry-run", action="store_true", help="只记录，不真正续跑")
    parser.add_argument("--status", action="store_true", help="打印当前状态")
    parser.add_argument("--reset", action="store_true", help="清空状态文件")
    parser.add_argument("--sessions", nargs="?", const=15, type=int, metavar="N",
                        help="列出最近 N 个 Codex 会话（默认 15），用于认领 UUID")
    parser.add_argument("--whitelist", action="store_true", help="查看 Codex 续跑白名单")
    parser.add_argument("--watch", metavar="ID", help="把会话加入白名单（UUID/前缀，或 last）")
    parser.add_argument("--unwatch", metavar="ID", help="把会话移出白名单")
    parser.add_argument("--mode", choices=["all", "whitelist"], help="设置续跑范围：all / whitelist")
    parser.add_argument("--version", action="version", version="quota-watch 1.0")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.dry_run:
        cfg["dry_run"] = True

    if args.sessions:
        print_sessions(cfg, max(1, int(args.sessions)))
        return 0

    if args.mode or args.whitelist or args.watch or args.unwatch:
        with QwLock(path_lock()) as got:
            if not got:
                print("另一个 quota-watch 正在运行，稍后重试", file=sys.stderr)
                return 3
            cfg = load_config()
            codex_cfg = cfg["codex"]
            if args.mode:
                codex_cfg["whitelist_mode"] = args.mode
                save_config(cfg)
                print(f"续跑范围已设为：{args.mode}")
            if args.unwatch:
                try:
                    tid = resolve_thread_id(cfg, args.unwatch)
                except AmbiguousThreadError as exc:
                    print(f"前缀 {exc.ref} 不唯一，请写更长的前缀或完整 UUID：", file=sys.stderr)
                    cat = load_thread_catalog()
                    for c in exc.candidates:
                        title = (cat.get(c) or {}).get("title") or ""
                        print("  - " + c + ("  (" + title + ")" if title else ""), file=sys.stderr)
                    return 2
                if not tid:
                    print(f"找不到会话：{args.unwatch}", file=sys.stderr)
                    return 2
                before = [str(x) for x in (codex_cfg.get("whitelist") or [])]
                after = [x for x in before if x.lower() != tid.lower()]
                codex_cfg["whitelist"] = after
                save_config(cfg)
                print(f"已移出白名单：{tid}" if len(after) != len(before) else f"本不在白名单：{tid}")
            if args.watch:
                try:
                    tid = resolve_thread_id(cfg, args.watch)
                except AmbiguousThreadError as exc:
                    print(f"前缀 {exc.ref} 不唯一，请写更长的前缀或完整 UUID：", file=sys.stderr)
                    cat = load_thread_catalog()
                    for c in exc.candidates:
                        title = (cat.get(c) or {}).get("title") or ""
                        print("  - " + c + ("  (" + title + ")" if title else ""), file=sys.stderr)
                    return 2
                if not tid:
                    print(f"找不到会话：{args.watch}", file=sys.stderr)
                    return 2
                wl = [str(x) for x in (codex_cfg.get("whitelist") or [])]
                if tid.lower() not in {x.lower() for x in wl}:
                    wl.append(tid)
                    codex_cfg["whitelist"] = wl
                    save_config(cfg)
                    print(f"已加入白名单：{tid}")
                else:
                    print(f"已在白名单中：{tid}")
            print()
            print_whitelist(load_config())
        return 0

    if args.status:
        print_status(cfg, load_state())
        return 0

    if args.reset:
        save_state({"version": 1, "entries": {}})
        print(f"已清空状态: {path_state()}")
        return 0

    if args.loop:
        log(f"quota-watch 启动（循环 {args.loop}s，dry_run={cfg.get('dry_run')}）")
        while True:
            with QwLock(path_lock()) as got:
                if got:
                    try:
                        run_once(cfg, bool(cfg.get("dry_run")))
                    except Exception as exc:  # 守护进程不能因为单次异常退出
                        log(f"扫描异常: {exc!r}", level="error")
                else:
                    log("另一个 quota-watch 正在运行，本轮跳过", level="warn")
            time.sleep(max(10, args.loop))

    with QwLock(path_lock()) as got:
        if not got:
            log("另一个 quota-watch 正在运行，退出", level="warn")
            return 3
        try:
            return run_once(cfg, bool(cfg.get("dry_run")))
        except Exception as exc:
            log(f"扫描异常: {exc!r}", level="error")
            return 1


if __name__ == "__main__":
    sys.exit(main())
