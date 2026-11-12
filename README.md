# quota-watch

**Codex / Claude Code 额度中断自动续跑守护**（macOS）

**中文** · [English](./README.en.md)

> 额度用尽把任务打断之后，不用再等到点手动点“继续”。quota-watch 会认出被打断的会话，算出额度恢复时间，到点自动把“继续”投回**原来的会话/窗口**。

`macOS` · `Python 3.10+` · 本地运行、不联网 · MIT

---

## 它解决什么问题

Claude Code 和 Codex 的额度窗口（5 小时 / 每周）会在长任务中途打断你：

- Codex 桌面 App 里，turn 会停在 `You've hit your usage limit ... try again at 1:16 AM`；
- Claude Code 交互式窗口里，会出现 `API Error: 429` / `当前并发数过高`。

人不在电脑前时，这一停可能就是一整夜。quota-watch 做三件事：

1. **定位**被打断的那个会话（Codex 精确到 thread + turn，Claude 精确到 tmux 会话）；
2. **算出恢复时间**（Codex 能读到精确到秒的 `resets_at`；Claude 从文案解析，解析不到就退避重试）；
3. **到点自动续跑**，并把每次决策写进日志。

## 功能

- Codex 桌面 App：解析 rollout JSONL 尾部事件，用官方 CLI 的 `codex queue --thread <id> --message "…" --resume` 把消息投回同一个会话；
- Claude Code：安装一个 `claude` 的 tmux 包装器，让交互式会话跑在 `qa-*` 窗口里，额度恢复后用 `tmux send-keys` 打回原窗口；
- **白名单**：只续跑你点名的会话（`qw --watch`），其余只记录不执行；
- `qw --sessions` 列出最近会话的 UUID / 标题 / 目录 / 状态，解决“哪个 UUID 是哪个会话”；
- 跳过子代理（subagent）会话；超过 24 小时没动静的旧中断不会突然复活；
- `--dry-run` 只记录不执行；macOS 通知；每轮决策都有可读日志。

## 要求

| 依赖 | 说明 |
|---|---|
| macOS | 用 launchd 常驻（每 60 秒一次）；其他平台可手动 `python3 qw.py --loop 30` |
| Python 3.10+ | 只用标准库 |
| Codex | 需要 Codex 桌面 App 或 `codex` CLI；续跑走 CLI 的本地 app-server daemon（守护会自动 `daemon start`，幂等） |
| tmux | 仅 Claude Code 侧需要（`brew install tmux`） |

## 安装

```bash
git clone https://github.com/chaos-zhou/quota-watch.git
cd quota-watch
./install.sh            # 幂等；可先 ./install.sh --dry-run 预览
```

安装会做四件事：

| 动作 | 位置 |
|---|---|
| 安装主程序、配置、便捷命令 `qw` | `~/.quota-watch/` |
| 安装 `claude` 的 tmux 包装器 | `~/.quota-watch/bin/claude` |
| 把 `~/.quota-watch/bin` 加进 PATH | `~/.zshrc` 的 `# >>> quota-watch claude shim >>>` 标记段 |
| 注册 launchd 定时任务（每 60 秒） | `~/Library/LaunchAgents/com.<user>.quota-watch.plist`（可用 `QW_LABEL` 覆盖） |

装完**新开一个终端**（或 `source ~/.zshrc`）让 `claude` 走包装器。

验证：

```bash
qw --status              # 当前状态、Codex daemon / App、tmux 会话
qw --sessions            # 最近会话列表（UUID / 标题 / 目录 / 是否限流）
tail -f ~/.quota-watch/logs/qw.log
```

## 日常使用

```bash
qw --status                 # 状态：等待中的续跑、下次动作时间、日志位置
qw --sessions [N]           # 最近 N 个会话（默认 15），用于认领 UUID
qw --whitelist              # 当前续跑范围与白名单
qw --watch last             # 把“最近一次被额度打断的会话”加入白名单
qw --watch '#2'             # 用 --sessions 列表编号加入
qw --watch "FDE"            # 用标题/目录关键字加入（唯一命中）
qw --unwatch <id>           # 移出白名单（正在等待的续跑会取消）
qw --mode all               # 全量模式：所有被打断的会话都续跑
qw --mode whitelist         # 白名单模式（默认建议）
qw --once --dry-run         # 只扫描记录，不真正续跑（先观察）
qw --loop 30                # 前台循环运行（launchd 之外的备选）
qw --reset                  # 清空状态文件
```

默认是**全量模式**；建议切到白名单，只放你真正在乎的会话：

```bash
qw --mode whitelist
qw --watch last
```

白名单按 thread id 精确匹配；`--watch/--unwatch` 支持唯一前缀、`--sessions` 编号、标题/目录关键字。加入白名单后，如果该会话还有未执行的续跑，下一次扫描（≤60 秒）会重新登记，仍按原恢复时间执行。

## 配置

`~/.quota-watch/config.json`，改完不用重启，下一次扫描生效：

```jsonc
{
  "notify": true,                    // macOS 通知
  "dry_run": false,                  // true = 只记录不执行
  "retention_days": 14,              // 状态保留天数
  "codex": {
    "enabled": true,
    "cli": "",                       // 留空自动探测：PATH 里的 codex → ChatGPT.app 内置
    "sessions_dir": "~/.codex/sessions",
    "lookback_hours": 96,            // 扫描多久以内的 rollout
    "max_interrupt_age_hours": 24,   // 超过这个时间没再写的旧中断不复活
    "resume_prompt": "【quota-watch 自动续跑】继续，从中断处接着做；不要重复已完成的部分，若已完成请直接说明。",
    "resume_mode": "queue",          // queue = 写回桌面 App；exec = 无界面 `codex exec resume`
    "ensure_daemon": true,           // 需要时自动启动 codex app-server daemon
    "whitelist_mode": "all",         // all | whitelist
    "whitelist": [],                 // whitelist 模式下的 thread id
    "max_attempts": 5,
    "backoff_min": [15, 30, 60, 120]
  },
  "claude": {
    "enabled": true,
    "tmux_prefix": "qa-",
    "user_activity_grace_sec": 180,  // 你刚在窗口里动过手就不插手
    "max_attempts": 8,
    "backoff_min": [10, 20, 40, 80, 160]
  }
}
```

想换续跑消息，改 `resume_prompt` 即可。

## 工作原理

### Codex（桌面 App）

不读正在写入的 sqlite，而是解析 `~/.codex/sessions/**/rollout-*.jsonl` 的尾部事件：

```json
{"type":"event_msg","payload":{"type":"task_complete","turn_id":"...",
 "error":{"message":"You've hit your usage limit ... try again at 2:33 PM",
          "codex_error_info":"usage_limit_exceeded"}}}
```

同一次 turn 的 `token_count` 事件里带精确恢复时间（unix 秒）：`rate_limits.primary.resets_at`（5 小时窗口）/ `secondary.resets_at`（每周窗口）。

恢复用官方命令 `codex queue --thread <uuid> --message "…" --resume`，依赖 CLI 自己的本地 app-server daemon（`~/.codex/app-server-control/`），不是桌面 App 的 socket；daemon 起来后排队消息会被立即执行。

> 有些会话很长（rollout 很大），`session_meta` 不在文件尾部——quota-watch 用文件名里的 UUID 兜底识别。

### Claude Code

Claude 的会话 JSONL 不记录 API 错误，所以改为观察 tmux 窗格：

- 包装器让**交互式** `claude` 跑在 `qa-<目录名>-<hash>` 的 tmux 会话里（`-p`、管道、`--version` 等一律直通，不套 tmux）；
- 每 60 秒 `tmux capture-pane` 看最后十几行，匹配 `429` / `rate limit` / `额度已用尽` / `当前并发数过高` 等；
- 出现“正在重试”类提示（`Retrying in 12s`、`esc to interrupt`）时**不**插手；
- 额度恢复后 `tmux send-keys` 把「继续」打回原来的窗口；
- 如果最近 3 分钟你在那个窗口里手动输入过，这一轮跳过。

## 安全与隐私

- **不联网**：全部本地运行；只读 `~/.codex/sessions/**`、`~/.codex/sqlite/codex.db`（会话标题目录，只读）、`~/.codex/queue_1.sqlite`（查重，只读）和 tmux 窗格；
- 不读取、不存储任何 API key / token；只看本机已有的登录态并调用本机 CLI；
- 会写入：`~/.quota-watch/`（程序/配置/日志/状态）、launchd plist、`~/.zshrc` 的一段标记；
- 想先观察：`qw --once --dry-run`，或配置 `"dry_run": true`；
- 随时可完全卸载（见下）。

## 已知限制

- 仅 macOS（launchd）；Linux 可以手动 `--loop`，但没有内置服务管理；
- Codex 侧需要桌面 App 或 `codex` CLI 的 daemon 在运行；App 关闭时消息会先排队，App/daemon 起来后执行，可能不是精确到点；
- 同时被打断多个会话时会各自续跑，抢同一个新额度窗口——用白名单控制范围；
- 它只负责“接着跑”，不校验跑出来的结果质量；
- `--sessions` 的标题来自 Codex 本地会话目录库，可能是会话早期的自动命名，或缺失（显示“无标题”）；
- Claude 侧的自动续跑依赖交互式 `claude` 走包装器（安装脚本已把 `~/.quota-watch/bin` 加进 PATH）。

## 目录结构

```
quota-watch/
├── qw.py                       # 主程序（单文件，标准库）
├── bin/qw                      # 便捷命令
├── bin/claude                  # claude 的 tmux 包装器
├── config.example.json         # 配置样例
├── install.sh / uninstall.sh   # 安装 / 卸载
├── launchd/quota-watch.plist.template
├── tests/test_detect.py        # 检测逻辑离线单测（19 个）
└── tests/test_integration.sh   # 端到端演练（假 rollout + 假 tmux）
```

## 测试

测试完全离线，不消耗任何模型额度、不改系统目录：

```bash
python3 tests/test_detect.py
bash tests/test_integration.sh
```

## 卸载

```bash
./uninstall.sh           # 保留 ~/.quota-watch（配置与日志）
./uninstall.sh --purge   # 连 ~/.quota-watch 一起删掉
```

会移除 launchd 任务和 `~/.zshrc` 里的标记段。

## License

[MIT](./LICENSE)

本项目与 OpenAI、Anthropic 无关联，只调用你本机已安装的 CLI 的公开命令。
