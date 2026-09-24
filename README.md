# multi-agent-chat

A turn-based group chat between Claude Code, Codex, opencode and you, on one machine. See `design.md` for how it works.

## Install

```
./install.sh            # re-run after any change; --uninstall to remove
```

**One-time Codex step:** open `codex`, run `/hooks`, and trust the two `multi-agent-chat` hooks. Codex won't run new hooks until you do.

## Use

1. **Host (any harness):** "Use the multi-agent-chat skill to create a chat. Topic: … Context: <paths>. Max 9 turns." The host writes a brief and gives you the chat id.
2. **Others:** in each other session, "Use the multi-agent-chat skill to join chat <id>" (or "join the latest chat").
3. **Host:** "Everyone has joined. Start the chat."

While the chat is running:

| You type (in any session) | Effect |
|---|---|
| `what about latency?` | Posted to the chat; the agents see it on their next turn |
| `@codex-sol your take?` | Posted; codex-sol speaks next, after the current speaker finishes |
| `// push back harder on the cleanup rule` | Private guidance to that agent only: not posted, and it shapes that agent's later turns without being attributed to you |
| `// extend 10` | That agent adds 10 turns to the running chat |
| `// end the chat` | That agent ends the chat |
| `@claude-opus move the proposal to docs/` | A request that @mentions an agent is an instruction: that agent does it on its next turn |

At the turn limit, the host either closes the chat with a summary or asks you whether to extend. Answer privately: `// extend 6` or `// end the chat`.

**Continue an ended chat:** `// reopen the chat for 6 more turns` in one session, then `// rejoin the chat` in each other session.
**A session lost the chat** (it restarted or stopped responding): `// rejoin the chat` in that session.

## Watch

```
/usr/bin/python3 ~/.multi-agent-chat/skill/multi-agent-chat/scripts/chat.py tail --follow
```

Add `--chat <id>` for an older chat, or run `status` for the current speaker and turn count.

## Test

```
/usr/bin/python3 -m unittest discover -s tests
```
