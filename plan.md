# multi-agent-chat — Implementation Plan

Implements `design.md`. Each stage has a check that must pass before the next stage starts.

## Repo layout

```
skill/multi-agent-chat/SKILL.md
skill/multi-agent-chat/scripts/chat.py # store, turns, relay decisions, hook entry point
adapters/claude/hooks.json            # merged into ~/.claude/settings.json
adapters/codex/hooks.json             # merged into ~/.codex/hooks.json
adapters/opencode/multi-agent-chat.js # linked from ~/.config/opencode/plugins/
install.sh                            # copies to ~/.multi-agent-chat, links harness dirs, merges hooks; --uninstall
tests/test_chat.py
```

## Stages

| # | Stage | Work | Check | Done by |
|---|---|---|---|---|
| 1 ✅ | Spikes S1–S4 | Throwaway probes, run in real sessions | Findings recorded in design.md. A spike passes only when a full wake-to-post cycle is observed. **If S1 fails, stop and ask you to choose the Codex waker** | Main agent + human |
| 2 ✅ | `chat.py` core | Schema, lobby/`brief`/`start`, rotation, `@name`, timeout skip, `max_turns`, relay decisions, `tail` | `tests/test_chat.py`: concurrent posts give gap-free `seq`; only the current speaker can post; a mid-turn `@name` doesn't interrupt the turn; a message arriving mid-reply (after `shown_seq`) is delivered next turn; a timeout skips the turn; `brief` is rejected after `start` and above 16 KB, and `join` prints it; every row of the relay table | Sonnet sub-agent |
| 3 ✅ | Skill | `SKILL.md` + `install.sh` | The skill shows up in all three harnesses. Given a topic plus a folder, the host writes a brief that follows the format; two Claude sessions complete a 4-turn chat on it. `--uninstall` leaves the config files JSON-equal to the originals | Main agent |
| 4 ✅ | Claude adapter | bind, relay | A public prompt is posted and blocked locally; a `//` prompt passes through; an injected prompt isn't re-posted; a failed relay blocks the prompt and shows an error | Sonnet sub-agent |
| 5 ✅ | opencode adapter | Plugin: bind, relay, wake | opencode takes its turn with no human input. Relay checks as in stage 4 | Sonnet sub-agent |
| 6 ✅ | Codex adapter | Hooks + the waker chosen in stage 1 | Same checks as stage 5 | Sonnet sub-agent |
| 7 ✅ | End-to-end | Claude hosts; Codex and opencode join via `--latest`; `max_turns` 9; the human interjects in each session | In the log: every human message appears exactly once, in order; a mid-turn `@name` is honored on the following turn; a killed waker leads to a skip; the chat ends automatically | Main agent + human |

Stages 4–6 can run in parallel once stage 3 is done.

## Decisions (approved 2026-09-23)

1. **Language:** only what comes with the Mac: `/usr/bin/python3` 3.9 standard library. TypeScript only for the opencode plugin.
2. **Scope:** one machine, discussion only.
3. **Install:** hooks go into the global harness configs. They do nothing unless a session is bound, and `--uninstall` reverts them.
4. **Codex runs with `--yolo`.** The risk is accepted (design § Security).

## End-to-end results (2026-09-23)

Two live 3-harness chats, driven through pseudo-terminals:

- **Chat 1 (Claude host, 6 turns):** create with a brief, join via `--latest`, start, a full rotation, and the chat ending automatically at `max_turns`.
- **Chat 2 (Codex host, 8 turns):**
  - A human message typed into opencode during Claude's turn with `@codex` was posted, and opencode's local copy was rewritten to "posted". Claude finished its turn, then Codex spoke next and answered it.
  - A human message typed into Codex was posted and blocked locally, and Codex received it only through turn delivery.
  - `//` prompts were answered locally in all three harnesses.

Bugs found and fixed during the run:
1. Codex hook entries were merged at the top level of `hooks.json` instead of under `"hooks"`.
2. The bind pattern captured `claude",` from JSON-encoded tool output.
3. Claude's background-task wake notification went through `UserPromptSubmit` and was relayed as a human message; `<task-notification>` prompts are now passed through.
4. Lobby prompts to the host were relayed; the relay now runs only while the chat is active.
5. `/end` is intercepted by every harness as its own slash command; it was replaced with the private `// end the chat`.
