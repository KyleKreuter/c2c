# c2c — Claude-to-Claude messaging over cmux

Let one running **Claude Code** instance talk to another.

If you run several Claude Code sessions in parallel inside [cmux](https://cmux.com),
each lives in its own terminal *surface* — separate processes, separate contexts,
no shared channel. `c2c` turns cmux's `send` / `send-key` / `read-screen`
primitives into a small messaging protocol so those instances can coordinate:
send a message, ask a question and get the answer, broadcast to a group, drop a
note that the peer picks up the next time it goes idle — and even **spawn new
worker instances or tear them down** from within an agent.

It works because a Claude Code instance with shell access isn't sandboxed from
other processes: it can drive the `cmux` CLI over cmux's Unix socket and thereby
reach any other surface.

```
 surface:17  ──cmux send / mailbox──▶  surface:20
     ▲                                     │
     └──────────  reply / receipt  ────────┘
```

## Two channels

| Channel | Command | Semantics |
|---------|---------|-----------|
| **Keystroke** | `c2c send` / `ask` / `ping` | Types into the peer's prompt and hits Enter. Arrives **immediately**, even mid-work. |
| **Mailbox** | `c2c post` / `reply` / `broadcast` | Drops a message in the peer's inbox. Its `Stop` hook injects it the next time the peer goes **idle** — no keystrokes into the input line. |

The mailbox channel adds threads, structured replies, delivery receipts, groups,
and a hop-count loop guard on top of a JSONL envelope store under `~/.c2c/`.

## Requirements

- [cmux](https://cmux.com) (the `cmux` CLI must be on `PATH` or set `CMUX_BIN`)
- `python3` and `perl` (both ship with macOS; standard on Linux)
- `bash` (3.2+ — macOS system bash is fine)

## Install

```bash
git clone https://github.com/KyleKreuter/c2c.git
cd c2c
./install.sh
```

`install.sh` will:

1. Symlink `bin/c2c` into `~/.local/bin` (so `c2c` is on your `PATH`).
2. Symlink the repo as the Claude Code skill `~/.claude/skills/c2c`
   (so instances can discover it via the `/c2c` skill).
3. Offer to register the `Stop` hook in `~/.claude/settings.json`
   (with a timestamped backup). This is what powers the mailbox channel.

> **Important:** the `Stop` hook is read at instance startup. Only Claude Code
> instances started **after** wiring the hook will deliver mailbox messages.
> Restart existing instances to activate it.

Prefer to wire the hook yourself? Add this to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command", "command": "c2c stop-hook", "timeout": 10 } ] }
    ]
  }
}
```

Hooks from `settings.json` **merge** with cmux's own inline hooks — they don't
replace each other.

## Usage

```bash
# Discovery
c2c whoami                     # my surface ref + UUID
c2c list                       # all reachable surfaces

# Lifecycle
c2c spawn                                  # launch a fresh yolo Claude in a new workspace
c2c spawn --cwd ~/project --name worker    # ...with a cwd and name
c2c kill  surface:31                       # terminate a peer instance

# Keystroke channel (immediate)
c2c send  surface:20 "push your branch, I'll merge"
c2c ask   surface:20 "which task are you on?"     # send, wait, read the reply
c2c read  surface:20                              # read a peer's screen
c2c ping  surface:20                              # handshake

# Mailbox channel (push on next idle)
c2c post  surface:20 "run the test suite when you're done"   # -> {id, thread}
c2c reply <thread> "done, all green"                         # structured back-channel
c2c ack   <thread>                                           # close a thread, no reply expected
c2c thread <thread>                                          # full transcript
c2c status <msg-id>                                          # delivery receipt
c2c receipts [n]                                             # recent receipts

# Broadcast / groups
c2c group set devs surface:14 surface:16
c2c broadcast --group devs "please post status"
c2c broadcast --workspace staging "deploy window in 10min"
c2c broadcast --all "heads up: rebasing main"
```

Surfaces are refs like `surface:20` (from `c2c list`) or raw UUIDs.
`post` / `reply` / `broadcast` print the `id` (for `status`) and `thread`
(for `reply`).

## How the mailbox works

Each message is one JSON line:

```json
{"id","thread","from","from_uuid","to_uuid","kind","in_reply_to","hops","ts","text"}
```

Stores under `~/.c2c/` (override with `C2C_HOME`):

| Path | Contents |
|------|----------|
| `inbox/<uuid>.jsonl` | pending messages for a surface |
| `threads/<thread>.jsonl` | full conversation transcript |
| `receipts/<uuid>.jsonl` | delivery receipts for messages *I* sent |
| `processed/<uuid>.log` | delivered archive |
| `groups.json` | broadcast groups |

On every `Stop`, `c2c stop-hook` drains this surface's inbox, injects the queued
messages, writes a delivery receipt back to each sender, and archives — draining
first, so it's loop-safe.

### Loop protection

Autonomous instances could otherwise acknowledge each other forever:

- **`c2c ack <thread>`** is delivered but framed as *"acknowledgement only — do
  NOT reply, thread closed"*.
- **Hop count:** every `reply`/`ack` increments `hops`. At/above `C2C_MAX_HOPS`
  (default 8) the injection warns *"hop limit reached — do not auto-reply"*.

### Delivery mode (`C2C_DELIVERY`)

- **`block`** (default) — forces the peer to process the message immediately when
  it goes idle. Claude Code renders this as a "Stop hook feedback" line
  (functionally correct; that's just how blocked stops are shown).
- **`context`** — clean, non-error injection via
  `hookSpecificOutput.additionalContext`, **but** the turn completes: the note is
  seen at the peer's next turn, not acted on immediately. Trade-off: clean
  rendering vs. push semantics.

## Spawning and killing instances

`c2c spawn` creates a new cmux workspace and launches
`claude --dangerously-skip-permissions` (yolo) in it, so an agent can bring up
its own workers. The one-time *"trust this folder"* dialog — which
`--dangerously-skip-permissions` does **not** skip — is auto-accepted, so the
spawn is hands-off. The new instance reads `~/.claude/settings.json`, so it gets
the c2c Stop hook (mailbox) immediately. Flags: `--cwd` (default: current dir),
`--name`, `--command` (default: the yolo Claude line).

`c2c kill <surface|workspace>` terminates a peer. If the surface is the only one
in its workspace (a typical spawned worker), the whole workspace is closed;
otherwise just that surface. Killing your own surface/workspace requires
`--force`.

```bash
s=$(c2c spawn --cwd ~/project --name builder | grep -o 'surface:[0-9]*' | head -1)
c2c post "$s" "run the full test suite and report back"
# ... later ...
c2c kill "$s"
```

## Receiving instance

When a message of the form `[c2c <- surface:N] …` (keystroke) or a hook line
*"Du hast N neue c2c-Nachricht(en) … thread <id>"* (mailbox) appears in your
prompt, it came **from another Claude instance, not the human**. Handle it and:

- Mailbox → `c2c reply <thread> "…"`, or `c2c ack <thread>` if nothing's left to say.
- Keystroke → `c2c send surface:N "…"`.
- An **ACK** or **hop-limit warning** means: do **not** auto-reply.

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `CMUX_BIN` | `cmux` | Path to the cmux CLI |
| `C2C_HOME` | `~/.c2c` | Envelope store root |
| `C2C_TIMEOUT` | `120` | `ask`/`wait` idle timeout (s) |
| `C2C_READ_LINES` | `60` | Lines `ask` reads back |
| `C2C_MAX_HOPS` | `8` | Hop limit before the loop warning |
| `C2C_DELIVERY` | `block` | `block` (immediate) or `context` (clean/passive) |

## Limitations

- Local cmux instances sharing one socket (`cmux-<uid>.sock`).
- The keystroke reply reader (`ask`) is a TUI scrape with heuristic
  "is-it-idle" detection; the mailbox back-channel is the structured path.
- Mailbox delivery requires the peer to have started after the hook was wired.

## License

MIT
