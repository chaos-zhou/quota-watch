# quota-watch

**Automatically resumes Codex / Claude Code sessions after a usage-limit reset** (macOS)

[中文](./README.md) · **English**

[![tests](https://github.com/chaos-zhou/quota-watch/actions/workflows/tests.yml/badge.svg)](https://github.com/chaos-zhou/quota-watch/actions/workflows/tests.yml)

> When a usage limit interrupts a long task, you no longer have to wait for the reset and click "continue" yourself. quota-watch identifies the interrupted session, works out when the limit resets, and sends "continue" back into the **same session / window** at that moment.

`macOS` · `Python 3.10+` · runs locally, no network · MIT

---

## The problem it solves

The usage windows (5-hour / weekly) of Codex and Claude Code regularly interrupt long-running work:

- In the Codex desktop app, a turn stops with `You've hit your usage limit ... try again at 1:16 AM`;
- In an interactive Claude Code terminal you get `API Error: 429` / `当前并发数过高` (too many concurrent requests).

If you are away from the keyboard, that can mean losing the whole night. quota-watch does three things:

1. **Locate** the interrupted session (Codex: exact thread + turn; Claude: exact tmux session);
2. **Compute the reset time** (Codex exposes a precise `resets_at`; for Claude it parses the message text and falls back to backoff retries);
3. **Resume at that time**, and write every decision to a log.

## Features

- Codex desktop app: parses the tail of the rollout JSONL and writes the message back into the same session with the official CLI command `codex queue --thread <id> --message "…" --resume`;
- Claude Code: installs a `claude` tmux wrapper so interactive sessions run in `qa-*` windows, then sends "continue" back with `tmux send-keys` after the reset;
- **Whitelist**: only resume the sessions you name (`qw --watch`); everything else is recorded but never executed;
- `qw --sessions` lists recent sessions with UUID / title / directory / state, solving "which UUID is which session";
- Skips subagent sessions; interrupts older than 24 hours are not revived;
- `--dry-run` logs only; macOS notifications; a readable log for every decision.

## Requirements

| Dependency | Notes |
|---|---|
| macOS | Runs as a launchd agent (every 60 s); on other platforms run `python3 qw.py --loop 30` manually |
| Python 3.10+ | Standard library only |
| Codex | Codex desktop app or the `codex` CLI; resuming goes through the CLI's local app-server daemon (the watcher runs `daemon start` automatically, idempotent) |
| tmux | Only needed for the Claude Code side (`brew install tmux`) |

## Install

```bash
git clone https://github.com/chaos-zhou/quota-watch.git
cd quota-watch
./install.sh            # idempotent; preview with ./install.sh --dry-run
```

The installer does four things:

| Action | Location |
|---|---|
| Installs the program, config and the `qw` shortcut | `~/.quota-watch/` |
| Installs the `claude` tmux wrapper | `~/.quota-watch/bin/claude` |
| Adds `~/.quota-watch/bin` to PATH | marked block in `~/.zshrc`: `# >>> quota-watch claude shim >>>` |
| Registers the launchd agent (every 60 s) | `~/Library/LaunchAgents/com.<user>.quota-watch.plist` (override with `QW_LABEL`) |

Open a **new terminal** (or `source ~/.zshrc`) afterwards so `claude` resolves to the wrapper.

Check it:

```bash
qw --status              # state, Codex daemon / app, tmux sessions
qw --sessions            # recent sessions (UUID / title / directory / limited or not)
tail -f ~/.quota-watch/logs/qw.log
```

## Everyday usage

```bash
qw --status                 # state: pending resumes, next action time, log paths
qw --sessions [N]           # last N sessions (default 15), to map UUIDs to sessions
qw --whitelist              # current scope and whitelist
qw --watch last             # whitelist the most recently interrupted session
qw --watch '#2'             # whitelist by index from --sessions
qw --watch "FDE"            # whitelist by unique title/directory keyword
qw --unwatch <id>           # remove from whitelist (cancels a pending resume)
qw --mode all               # all sessions are resumed
qw --mode whitelist         # whitelist only (recommended)
qw --once --dry-run         # scan and log only, never resume (good first run)
qw --loop 30                # run in the foreground (alternative to launchd)
qw --reset                  # clear the state file
```

The default is **all mode**; switching to a whitelist is recommended:

```bash
qw --mode whitelist
qw --watch last
```

Whitelist entries are matched by exact thread id. `--watch` / `--unwatch` also accept a unique prefix, a `--sessions` index, or a title/directory keyword. If a session is added to the whitelist while it still has a pending resume, the next scan (≤60 s) re-registers it and still resumes at the original reset time.

## Configuration

`~/.quota-watch/config.json` — changes take effect on the next scan, no restart needed:

```jsonc
{
  "notify": true,                    // macOS notifications
  "dry_run": false,                  // true = log only
  "retention_days": 14,              // how long to keep state entries
  "codex": {
    "enabled": true,
    "cli": "",                       // empty = auto-detect: `codex` on PATH → bundled in ChatGPT.app
    "sessions_dir": "~/.codex/sessions",
    "lookback_hours": 96,            // how far back to scan rollouts
    "max_interrupt_age_hours": 24,   // do not revive interrupts older than this
    "resume_prompt": "【quota-watch 自动续跑】继续，从中断处接着做；不要重复已完成的部分，若已完成请直接说明。",
    "resume_mode": "queue",          // queue = write back to the desktop app; exec = headless `codex exec resume`
    "ensure_daemon": true,           // start the codex app-server daemon when needed
    "whitelist_mode": "all",         // all | whitelist
    "whitelist": [],                 // thread ids used in whitelist mode
    "max_attempts": 5,
    "backoff_min": [15, 30, 60, 120]
  },
  "claude": {
    "enabled": true,
    "tmux_prefix": "qa-",
    "user_activity_grace_sec": 180,  // skip this round if you just typed in that window
    "max_attempts": 8,
    "backoff_min": [10, 20, 40, 80, 160]
  }
}
```

To change the resume message, edit `resume_prompt`.

## How it works

### Codex (desktop app)

It never reads the sqlite files that are being written; instead it parses the tail of `~/.codex/sessions/**/rollout-*.jsonl`:

```json
{"type":"event_msg","payload":{"type":"task_complete","turn_id":"...",
 "error":{"message":"You've hit your usage limit ... try again at 2:33 PM",
          "codex_error_info":"usage_limit_exceeded"}}}
```

The `token_count` event of the same turn carries the exact reset time (unix seconds): `rate_limits.primary.resets_at` (5-hour window) / `secondary.resets_at` (weekly window).

Resuming uses the official command `codex queue --thread <uuid> --message "…" --resume`, which depends on the CLI's own local app-server daemon (`~/.codex/app-server-control/`), not the desktop app's socket. Once the daemon is up, queued messages are executed immediately.

> Some sessions are very long (large rollouts), so `session_meta` is not in the tail anymore — quota-watch falls back to the UUID in the rollout filename.

### Claude Code

Claude session JSONL files do not record API errors, so quota-watch watches tmux panes instead:

- The wrapper makes **interactive** `claude` run in a `qa-<dir>-<hash>` tmux session (`-p`, pipes, `--version` etc. pass through without tmux);
- Every 60 s it runs `tmux capture-pane` on the last dozen lines and matches `429` / `rate limit` / `额度已用尽` / `当前并发数过高` etc.;
- "Retrying" hints (`Retrying in 12s`, `esc to interrupt`) are **not** interrupted;
- After the reset, `tmux send-keys` sends "continue" back into the original window;
- If you typed in that window within the last 3 minutes, that round is skipped.

## Security & privacy

- **No network access**: everything runs locally. It only reads `~/.codex/sessions/**`, `~/.codex/sqlite/codex.db` (session titles, read-only), `~/.codex/queue_1.sqlite` (pending-message dedupe, read-only) and tmux panes;
- It never reads or stores API keys / tokens; it only reuses the login state already present on your machine and invokes the local CLIs;
- It writes: `~/.quota-watch/` (program / config / logs / state), the launchd plist, and one marked block in `~/.zshrc`;
- To observe first: `qw --once --dry-run`, or set `"dry_run": true`;
- Fully uninstallable at any time (see below).

## Known limitations

- macOS only (launchd); on Linux you can run `--loop` manually, but there is no built-in service management;
- The Codex side needs the desktop app or the `codex` CLI daemon running; if the app is closed, the message is queued first and runs when the app/daemon comes back — which may not be exactly at the reset moment;
- Several sessions interrupted at once will each resume and compete for the same fresh quota window — use the whitelist to control the scope;
- It only "keeps going"; it does not review the quality of the work produced;
- Titles in `--sessions` come from Codex's local session catalog and may be an early auto-generated name, or missing (shown as "no title");
- Claude auto-resume relies on interactive `claude` going through the wrapper (the installer adds `~/.quota-watch/bin` to PATH).

## Repository layout

```
quota-watch/
├── qw.py                       # main program (single file, stdlib only)
├── bin/qw                      # `qw` shortcut
├── bin/claude                  # tmux wrapper for claude
├── config.example.json         # example config
├── install.sh / uninstall.sh   # install / uninstall
├── launchd/quota-watch.plist.template
├── tests/test_detect.py        # offline unit tests for detection logic (19 tests)
└── tests/test_integration.sh   # end-to-end drill (fake rollout + fake tmux)
```

## Tests

The tests are fully offline: they consume no model quota and touch no system directories.

```bash
python3 tests/test_detect.py
bash tests/test_integration.sh
```

## Uninstall

```bash
./uninstall.sh           # keeps ~/.quota-watch (config and logs)
./uninstall.sh --purge   # also removes ~/.quota-watch
```

This removes the launchd agent and the marked block in `~/.zshrc`.

## License

[MIT](./LICENSE)

This project is not affiliated with OpenAI or Anthropic; it only calls the public commands of the CLIs already installed on your machine.
