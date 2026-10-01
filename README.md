# multi-agent-chat

A turn-based group discussion between AI agents from different harnesses (Claude Code, Codex, opencode) and you, on one machine. One agent hosts. You start each agent in its own session, and anything you send with `//` reaches all of them. How it works: `design.md`.

## Requirements

- macOS with Xcode Command Line Tools (`/usr/bin/python3` 3.9). Nothing else to install.
- Any of Claude Code, Codex and opencode.

## Install

```
git clone https://github.com/ducduong/multi-agent-chat.git
cd multi-agent-chat
./install.sh
```

What it does:
- Copies the skill and adapters to `~/.multi-agent-chat/`. Chat data lives in `~/.multi-agent-chat/data/chat.db`.
- Links the skill into `~/.claude/skills`, `~/.codex/skills` and `~/.config/opencode/skills`, and installs the opencode plugin.
- Adds hooks to `~/.claude/settings.json` and `~/.codex/hooks.json`: relay (`UserPromptSubmit`), bind (`PostToolUse`) and the safety net (`Stop`). Existing hooks are kept, and the originals are backed up to `~/.multi-agent-chat/backup/`.

After installing:
1. **Codex:** open `codex`, run `/hooks`, and trust the three `multi-agent-chat` hooks. Codex runs new or changed hooks only after you trust them.
2. **Restart running sessions.** Claude Code and Codex load hooks, and agents load the skill, when a session starts.

**Update:** `git pull && ./install.sh`. This is safe while a chat is running; then repeat steps 1–2 if hooks changed.
**Uninstall:** `./install.sh --uninstall` removes the hooks, links and plugin, and keeps `data/` and the backups.

## Start a chat

1. **Host**, in any harness: "Use the multi-agent-chat skill to create a chat. Topic: … Context: <paths or URLs>. Max 20 turns." The host reads the context, writes a brief, and gives you the chat id.
2. **Each other agent**, in its own session: "Use the multi-agent-chat skill to join chat <id>" (or "…join the latest chat").
3. **Host:** "Everyone has joined. Start the chat." The host speaks first.

Agents name themselves `<harness>-<model>`, for example `claude-opus`, `claude-fable` or `codex-sol`. Two sessions from the same harness can join one chat.

## Talking during a chat

A normal message in a session is a **private, direct chat with that agent**. Start with `//` to post to the **group**.

| You type (in any session) | Effect |
|---|---|
| `push back harder on the cleanup rule` | Private guidance: not posted, and it shapes that agent's later turns without being attributed to you |
| `what's your honest read so far?` | Private question; only that agent answers |
| `extend 10` | That agent adds 10 turns |
| `end the chat` | That agent ends the chat |
| `// what about latency?` | Posted to the group; each agent sees it on its next turn |
| `// @codex-sol your take?` | Posted; codex-sol speaks next, after the current speaker finishes |
| `// @claude-opus move the proposal to docs/` | A group message that @mentions an agent is an instruction: that agent does it on its next turn |

Tips:
- Post `//` messages from a Claude or Codex session. opencode can't block a prompt, so a `//` typed there costs one tiny "posted" reply from its model.
- An agent never acts on another agent's request to change files. Only your messages carry instructions.

## How turns work

- **Rotation:** agents speak one at a time in join order. Your messages never use a turn.
- **Pass:** an agent with nothing new to add passes. A pass is free: nothing is posted and no turn is used.
- **Pause:** when every agent passes in a row (for example, all waiting for you), the chat pauses. There are no turns and no model calls until your next `//` message, which resumes it.
- **Timeout:** an agent that doesn't respond within 10 minutes is skipped. A skip counts as a pass, so an absent agent leads to a pause, not a loop.
- **Turn limit:** the chat doesn't end automatically. At the limit, the host either posts a closing summary, which ends the chat, or asks whether to extend. Just answer it: `extend 6` or `end the chat`.

## Watch

```
alias mac='/usr/bin/python3 ~/.multi-agent-chat/skill/multi-agent-chat/scripts/chat.py'   # add to ~/.zshrc
mac tail --follow      # live view of the latest chat (lobby too); --chat <id> for another
mac status             # status, participants, current speaker, turns used / limit
```

## When something goes wrong

| Symptom | Fix |
|---|---|
| An agent stopped responding, or never takes its turn | Tell it: `rejoin the chat` (or `resume the chat`). It retakes its seat and gets everything it missed |
| A session was restarted mid-chat | Same: `rejoin the chat` |
| The chat ended but you want more | Tell one agent `reopen the chat for 6 more turns`, then tell each other agent `rejoin the chat` |
| A new hook doesn't seem to run | Restart the session. In Codex, also trust the hook in `/hooks` |
| Codex shows "Hook failed … exited with code 1" | Run the hook command from `~/.codex/hooks.json` by hand to see which hook fails. The multi-agent-chat hooks exit 0 when they work |

## Keeping costs down

Every model call re-reads the agent's whole session history.
- **Use a fresh session per chat.** A long-running session can carry hundreds of thousands of tokens into every turn.
- **Let idle chats pause.** They cost nothing while paused.
- **A Claude turn takes 2 model calls, and a Codex turn 2.** Prefer fewer, more substantive turns: set a sensible `Max N turns` and extend when needed.

## Command reference

Agents run these through the skill. You only need `tail` and `status`, but everything is available. `--chat` defaults to the latest chat.

| Command | Purpose |
|---|---|
| `create --name N --topic T [--max-turns K] [--harness H]` | Create a chat in the lobby; the caller is the host |
| `brief --chat C --name N --file PATH\|-` | Host sets the brief (lobby only, max 16 KB) |
| `join C\|--latest --name N [--harness H] [--rejoin]` | Join the lobby, or `--rejoin` to retake an existing seat |
| `start --chat C --name N` | Host starts the chat |
| `wait --chat C --name N [--timeout S]` | Block until it's N's turn (`0` = no timeout); prints new messages |
| `post --chat C --name N TEXT\|--file PATH\|- [--and-wait]` | Post on your turn; `--and-wait` then waits for the next turn |
| `pass --chat C --name N [--and-wait]` | Pass on your turn (free) |
| `extend --chat C --name N --turns K` | Add K turns (any participant; active or paused) |
| `reopen --chat C --name N [--turns K]` | Continue an ended chat; N speaks first |
| `end --chat C [--name N]` | End the chat |
| `status [--chat C]` / `tail [--chat C] [--follow]` | Inspect or watch |

Internal: `hook <harness> relay|bind|stop` (called by the harness hooks) and `waker codex` (Codex wake loop).

## Configuration

`~/.multi-agent-chat/config.json`:

| Key | Default | Meaning |
|---|---|---|
| `max_turns` | 12 | Turn limit when the host doesn't set one |
| `turn_timeout_s` | 600 | Seconds before an unresponsive agent is skipped |
| `wait_timeout_s` | 300 | `wait`'s timeout when `--timeout` is omitted |

## Development

```
/usr/bin/python3 -m unittest discover -s tests     # full suite
./install.sh                                       # deploy local changes
```

Layout:
- `skill/multi-agent-chat/`: `SKILL.md` and `scripts/chat.py`, the only code that touches the store.
- `adapters/`: Claude and Codex hook definitions, and the opencode plugin.
- `design.md`: architecture and decisions. `plan.md`: build plan and test results.
