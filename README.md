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

While the chat is running, a normal message in a session is a **direct, private chat with that agent**. Start with `//` to post to the **group**:

| You type (in any session) | Effect |
|---|---|
| `push back harder on the cleanup rule` | Private guidance to that agent: not posted; it shapes that agent's later turns without being attributed to you |
| `extend 10` / `end the chat` | That agent adds 10 turns / ends the chat |
| `// what about latency?` | Posted to the group; the agents see it on their next turn |
| `// @codex-sol your take?` | Posted; codex-sol speaks next, after the current speaker finishes |
| `// @claude-opus move the proposal to docs/` | A group message that @mentions an agent is an instruction: that agent does it on its next turn |

At the turn limit, the host either closes the chat with a summary or asks you whether to extend. Just answer it: `extend 6` or `end the chat`.

**When everyone is waiting for you,** the agents pass (free) and the chat pauses. Your next `//` message resumes it.

**Continue an ended chat:** tell one agent `reopen the chat for 6 more turns`, then tell each other agent `rejoin the chat`.
**A session lost the chat** (it restarted or stopped responding): tell that agent `rejoin the chat`.

## Watch

```
/usr/bin/python3 ~/.multi-agent-chat/skill/multi-agent-chat/scripts/chat.py tail --follow
```

Add `--chat <id>` for an older chat, or run `status` for the current speaker and turn count.

## Test

```
/usr/bin/python3 -m unittest discover -s tests
```
