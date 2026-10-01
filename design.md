# multi-agent-chat — Design

## Goal

2–3 AI agents from different model families and harnesses (Claude Code, Codex, opencode; pi later) plus one human hold a single discussion. The human starts every session. Anything the human types in any session reaches every agent.

## Scope (v0)

- In: one machine, discussion only, turn-based, Claude Code + Codex + opencode.
- Out: networking, shared-repo editing, pi, a UI beyond a terminal tail, context compaction.

## Architecture

```
            ┌─── ~/.multi-agent-chat/data/chat.db (SQLite) ──┐
            │ chats · participants · messages · sessions     │
            └──────▲───────────────▲───────────────▲─────────┘
                   │ chat.py       │ chat.py       │ chat.py
  ┌────────────────┴──┐  ┌─────────┴─────────┐  ┌──┴────────────────┐
  │ Claude Code       │  │ Codex             │  │ opencode          │
  │ skill + hooks     │  │ skill + hooks     │  │ skill + plugin    │
  │ waker: background │  │ waker: codex      │  │ waker: plugin     │
  │ Bash wait         │  │ queue (S1)        │  │ promptAsync       │
  └───────────────────┘  └───────────────────┘  └───────────────────┘
```

1. **`chat.py`** is one script using only the Python standard library. It is the only thing that touches the store, and it decides turns, delivery and how human prompts are handled.
2. **The skill** is one `SKILL.md` shared by all harnesses. It covers how to create, join or start a chat and take a turn, plus the rules.
3. **Harness adapters** are thin hooks or plugins calling `chat.py hook <harness> <event>`. They do three jobs:
   - **bind:** link the session to a chat participant on `create`/`join`
   - **relay:** send the human's typed prompts to `chat.py`
   - **wake:** start a turn in an idle session

## Runtime

Use only what comes with a developer Mac.

- **`chat.py` targets `/usr/bin/python3` (3.9.6).** It comes with Xcode Command Line Tools, which git and Homebrew already need. The code must run on 3.9: no `match` statements, no `X | Y` type hints.
- **The opencode plugin is plain JavaScript because opencode plugins must be JS/TS.** It runs on opencode's built-in runtime with no build step.

## Home

Everything installed lives in one folder. The repo holds the source, and `install.sh` copies it here:

```
~/.multi-agent-chat/
  config.json                 # defaults: max_turns 12, turn_timeout_s 600, wait_timeout_s 300 (wait's default when --timeout is omitted)
  skill/multi-agent-chat/     # the original skill: SKILL.md + scripts/chat.py
  adapters/                   # claude + codex hook snippets, opencode plugin source
  data/chat.db
```

The harnesses point back to this folder rather than keeping their own copies:

| Harness | Link or config | Points to |
|---|---|---|
| Claude Code | `~/.claude/skills/multi-agent-chat` | the skill |
| Codex | `~/.codex/skills/multi-agent-chat` | the skill |
| opencode | `~/.config/opencode/skills/multi-agent-chat` | the skill |
| opencode | `~/.config/opencode/plugins/multi-agent-chat.ts` | `adapters/` |
| Claude Code, Codex | hook entries in their settings | `chat.py hook …` |

`config.json` is plain JSON because Python 3.9 has no built-in TOML reader.

## Store

One SQLite file, `data/chat.db`, with one transaction per command. SQLite is chosen over JSONL plus a lock file because it is less code: it updates messages and turn state together in one step.

| Table | Columns |
|---|---|
| `chats` | id (`20260923-7f3a`), topic, brief, host, status (`lobby`/`active`/`ended`), speaker, turn_started_at, next_speaker, turns_taken, max_turns |
| `participants` | chat_id, name, harness, position, shown_seq, read_seq |
| `messages` | chat_id, seq, ts, from, kind (`human`/`agent`/`system`), via, text |
| `sessions` | harness, session_id, chat_id, name |

## chat.py commands

| Command | Behavior |
|---|---|
| `create --name claude --topic "..." [--max-turns 12]` | Creates the chat in `lobby` status, joins the caller as host and prints the chat id |
| `brief (--file <path> \| -)` | Host only, lobby only. Sets the brief (at most 16 KB) |
| `join (<id> \| --latest) --name codex` | Adds a participant (lobby only) and prints the topic, brief and roster |
| `join <id> --name N --rejoin` | Retake an existing seat from a new or recovered session (lobby or active). The session is re-bound; the agent gets everything after its `read_seq` |
| `reopen --chat <id> --name N [--turns K]` | Any participant, on an ended chat. The chat becomes active with K more turns and N speaks first. Other agents rejoin |
| `start` | Host only. Fixes the roster, sets status to `active` and gives the first turn to the host |
| `wait --name codex [--timeout 300] [--skip-turn K]` | Blocks until it's this agent's turn (other than turn K, which a waker has already delivered), then prints the messages after its `read_seq` and sets `shown_seq` to the last one printed. Exit code: 0 = your turn, 3 = ended, 4 = timeout |
| `post --name codex "<text>"` | Accepted only if the caller is the current speaker. Sets `read_seq = shown_seq` and passes the turn on |
| `waker codex --thread T` | Codex only: the detached wake loop started by bind |
| `extend --name N --turns K` | Any participant, while the chat is active: raises `max_turns` by K. The human asks any agent directly (`extend K`); the host also uses it from the limit turn |
| `resume --name <host>` | Host only, on a paused chat: active again, host speaks first, dead Codex wakers restarted |
| `pass --name N` | The current speaker passes: no message, no turn used. A full round of passes pauses the chat |
| `status` / `tail [--follow]` / `end` | Show state / show the chat for humans / end the chat |
| `hook <harness> <event>` | Adapter entry point: hook JSON on stdin, the harness's expected response on stdout |

## Topic and brief

The human gives the host the topic, and any pointers to context, in the host's session. For example: "start a chat on whether to split the billing service; context is in ~/code/billing and docs/adr/". The host then:

1. Runs `create --topic "<the human's topic, as stated>"`.
2. Gathers context by reading the files, repos and docs the human pointed to, plus anything obviously related.
3. Writes a brief and sets it with `brief --file`, then gives the human the chat id.

What the brief contains:

| Section | Content |
|---|---|
| Goal | What the discussion should decide or produce |
| Background | Key facts and constraints, summarized |
| References | Absolute file paths and URLs, each with one line on why it matters |
| Open questions | Where the discussion should start |

The brief summarizes and points to sources rather than copying file contents. Joining agents are on the same machine and open the references themselves when needed, so each agent's context stays small. Once the chat has started the brief can't change, and new context goes into the host's turns.

## Participant names

Names are `<harness>-<model>` in lowercase, with a short model word and no version: `claude-opus`, `claude-fable`, `codex-sol`. Two sessions from the same harness are then easy to tell apart and to address, for example `@claude-fable`. Each agent picks its own name. One unsure of its model uses just the harness name. Names match `[a-z0-9][a-z0-9_-]*[a-z0-9]`, at most 32 characters, and `@mentions` are case-insensitive.

## Turns

- **Lobby first.** `create` → `brief` → the others `join` → the human tells the host to `start`. The first turn is the host's opening.
- **Rotation** follows join order: `claude → codex → opencode → claude …`
- **Only the current speaker can post.** A duplicate post from a double wake is rejected, because the turn has already moved on.
- **`@name` from the human picks the next speaker. It never interrupts the current turn.** If Codex is speaking and the human types `@claude challenge that`, Codex finishes, Claude speaks next, and rotation continues from Claude.
- **Human messages never use a turn.**
- **Passes are free.** An agent with nothing to add runs `pass`: no message is posted, and it doesn't count against `max_turns`. A timeout skip counts as a pass, not a turn.
- **A full round of passes pauses the chat,** for example when everyone is waiting for the human.
  - While paused: no speaker, no timeouts, no model calls.
  - The human's next `//` message resumes it, going to the mentioned agent or the next in rotation. The host can also `resume` it when the human asks in its session. The host then speaks first, and the others are woken by rotation, since their waits keep blocking while paused. `resume` also restarts dead Codex wakers. Only the host can resume, and only on the human's direction, so a pass loop doesn't restart itself.
  - Why: each pass costs a full model call over the agent's session history (measured at about 1.26M cached tokens per pass for a long Claude session), and it used to consume the turn budget.
- **A turn with no post within 10 min is skipped,** with a system message, and counts as a pass. The check runs whenever any `chat.py` command runs, so no background process is needed.
- **Reaching `max_turns` doesn't end the chat.** It gives the host a *limit turn*, which has no timeout, so the chat waits for the human.
  - If the discussion is finished, the host posts a closing summary, which ends the chat.
  - Otherwise the host asks the human in its session whether to extend. The human answers the host directly (`extend 6` or `end the chat`); normal prompts stay private to that agent. The host then runs `extend --turns 6` and continues its turn, or runs `end`.
  - Since the host always makes the final call, the conclusion is never split between a summary and a later correction.
- **The chat also ends** when the human tells any agent `end the chat` and it runs `end`. There's no `/end` keyword: Claude Code, Codex and opencode all treat `/…` as their own commands before any hook sees it.

## Delivery

- `wait` prints every message after the agent's `read_seq`. `read_seq` moves forward only when the agent posts, and only up to `shown_seq`. Example: Codex is shown messages up to 10, the human sends 11, Codex posts. Its `read_seq` becomes 10, so it gets 11 on its next turn.
- **A failed wake loses nothing.** The turn is eventually skipped, and the messages are delivered on the agent's next turn.
- **Messages arriving mid-reply go out next turn.** Anything that arrives while the agent is writing comes after what it was shown, so it's included in its next delivery.
- A waker injects messages prefixed with `[multi-agent-chat]`, and labels each one, for example `[12] codex: …`, so the agent reads them as quoted chat content.

## Human input

A normal prompt in a session is a direct, private chat with that agent. A prompt starting with `//` goes to the group. Most prompts are private conversation with the local agent, so that's the default and posting is the explicit act. `chat.py hook <harness> relay` decides each prompt in a bound session:

| The human types | Result | Local harness |
|---|---|---|
| `push back harder on the cleanup rule` | Private: not posted | Passed through; the agent answers and applies it later without attributing it |
| `end the chat` / `extend 10` | Private; the agent runs `end` / `extend` | Passed through |
| `// what about latency?` | Posted as `human` with the `//` removed | **Blocked**, so the agent doesn't answer out of turn; it gets the message on its next turn |
| `// @opencode-deepseek your take?` | Posted; that agent speaks next | Blocked |
| `//` alone | Nothing posted | Blocked with "empty group message" |
| Anything while the chat is in the lobby, or in a session not in a chat | Ignored | Passed through |

- **If relaying a `//` message fails** (for example `chat.py` errors or the database is locked), the prompt is blocked and the human sees the error, rather than the agent answering it as a private prompt.
- Claude Code and Codex can block a prompt with `UserPromptSubmit`. opencode can't, so the plugin rewrites it to `[multi-agent-chat] … Reply with exactly: posted` (S3).
- Injected wake prompts (`[multi-agent-chat] …`) and Claude's `<task-notification>` prompts don't start with `//`, so they pass through with no special case.

## Waking agents

Every harness runs the same loop: **`wait` returns → messages go to the agent → the agent posts → wait again.** A wake counts only when the post goes through.

- **Claude Code:** the agent posts with `post --and-wait` as one background command, which posts and then waits for its next turn (`wait --timeout 0` after joining). Claude Code re-invokes the agent when the command exits. A turn costs 2 model calls, and an idle or paused chat costs none. The session stays idle and free for the human to type into.
  - Hooks: `UserPromptSubmit` → relay; `PostToolUse` (Bash) → bind; `Stop` → safety net.
  - The Stop hook blocks an agent from ending its response while it still holds the turn, or (Claude) while no wait is running. `wait` records its pid, and a 5 s grace covers a wait that was just launched. The hook passes on the host's limit turn and on a second consecutive stop, to avoid loops.
  - Claude Code loads hooks when a session starts, so a session that predates an install doesn't have new hooks.
- **opencode:** the plugin (`~/.config/opencode/plugins/multi-agent-chat.js`) uses `tool.execute.after` → bind and `chat.message` → relay. A blocked prompt is rewritten rather than blocked (see S3). Its wake loop runs `chat.py wait --skip-turn <last>` → `client.session.promptAsync(...)`, so each turn is delivered once.
- **Codex:** hooks in `~/.codex/hooks.json`: `UserPromptSubmit` → relay; `Stop` → safety net (turn check only); `PostToolUse` → bind, which starts a detached waker (`chat.py waker codex`, one per participant) that loops `wait --skip-turn <last>` → `codex queue --thread <session_id> --message "[multi-agent-chat] …"`.

Reported bugs mean injection alone doesn't prove a wake. There are reports of `codex queue` not running messages for unloaded threads, and of `promptAsync` saving messages without starting a turn on busy sessions. The spikes test the full cycle through to a post.

## Security

v0 trusts the local agents. Any agent with shell access can run `chat.py`, so message kinds are not authenticated. The skill says agents' messages are quoted content, never instructions. A human message that @mentions an agent is an instruction to that agent, carried out on its turn. This relies on only the relay hook creating `human` messages, which v0's trust model accepts. Codex runs with `--yolo` (the human's standard setup), so no sandbox limits it: a Codex that another agent talks into running a command faces no barrier. This risk is accepted for v0, and the scope is discussion only.

## Spike findings (2026-09-23)

| Spike | Result |
|---|---|
| **S1** `codex queue` | **Pass.** An idle session ran the queued message within about 8 s. A busy session ran it right after the current turn. A closed session accepted it but never ran it, and the turn timeout skips that agent |
| **S2** hooks | **Pass.** Claude Code and Codex send the same fields: `session_id`, `prompt`, `tool_name` (`Bash`), `tool_input.command` and `tool_response` (an object with `stdout` in Claude, a string in Codex). `{"decision":"block","reason":…}` blocks the prompt in both, and `//` prompts pass through. **Codex runs new hooks only after they're approved** with `/hooks` in the TUI (stored as `hooks.state.<key>.trusted_hash` in `config.toml`). This is a one-time human step after install |
| **S3** opencode | **Pass, with one change.** `promptAsync` starts a turn in an idle session (about 2 s), and in a busy session it runs after the current turn. Throwing in `chat.message` does **not** block a prompt. Rewriting `output.parts[].text` does replace what the model sees, so blocking works by rewriting the prompt to "reply with exactly: posted", at the cost of one tiny turn. The plugin is plain JS, so there's no build step. Prompts sent via `opencode run` arrive wrapped in quotes, which relay strips |
| **S4** Claude background wake | This session's background tasks re-invoked an idle session; the same mechanism runs background `wait`. Covered by end-to-end testing rather than a separate spike |

## Watching

`python3 ~/.multi-agent-chat/skill/multi-agent-chat/scripts/chat.py tail --follow` in any terminal shows the chat live (add `--chat <id>` for an older chat). No web UI in v0.
