// opencode plugin: relays human prompts and wakes agents via chat.py (see design.md).
// Plain JS, no build step -- opencode loads plugin files directly.
import { spawn } from "node:child_process";
import { appendFile, mkdir } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";

const HOME = homedir();
const CHAT_PY = path.join(HOME, ".multi-agent-chat", "skill", "multi-agent-chat", "scripts", "chat.py");
const LOG_PATH = path.join(HOME, ".multi-agent-chat", "data", "opencode-plugin.log");
const PYTHON = "/usr/bin/python3";
const BIND_RE = /MAC_BIND chat=([0-9]{8}-[0-9a-f]{4}) name=([a-z0-9][a-z0-9_-]*)/;
const RETRY_DELAY_MS = 5000;

async function logError(message) {
  try {
    await mkdir(path.dirname(LOG_PATH), { recursive: true });
    await appendFile(LOG_PATH, new Date().toISOString() + " " + message + "\n");
  } catch (_e) {
    // logging is best-effort; never let it throw into the TUI
  }
}

function runChatPy(args, input) {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, [CHAT_PY, ...args]);
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => { stdout += d; });
    child.stderr.on("data", (d) => { stderr += d; });
    child.on("error", reject);
    child.on("close", (code) => resolve({ code, stdout, stderr }));
    if (input !== undefined) child.stdin.write(input);
    child.stdin.end();
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export const MultiAgentChat = async ({ client }) => {
  const wakeLoops = new Map(); // sessionID -> running

  async function wakeLoop(sessionID, chatId, name) {
    let lastTurn = null;
    while (true) {
      const args = ["wait", "--chat", chatId, "--name", name, "--timeout", "300"];
      if (lastTurn !== null) args.push("--skip-turn", String(lastTurn));

      let result;
      try {
        result = await runChatPy(args);
      } catch (e) {
        await logError("wake loop wait failed for " + chatId + "/" + name + ": " + e);
        await sleep(RETRY_DELAY_MS);
        continue;
      }

      if (result.code === 3) break; // chat ended
      if (result.code === 4) continue; // timed out, poll again
      if (result.code !== 0) {
        await logError("wake loop wait exited " + result.code + " for " + chatId + "/" + name +
          ": " + result.stderr);
        await sleep(RETRY_DELAY_MS);
        continue;
      }

      const text = result.stdout;
      const m = text.match(/your turn \((\d+)\/\d+\)/);
      const turn = m ? parseInt(m[1], 10) : null;
      try {
        await client.session.promptAsync({
          path: { id: sessionID },
          body: { parts: [{ type: "text", text }] },
        });
        lastTurn = turn;
      } catch (e) {
        await logError("promptAsync failed for " + chatId + "/" + name + ": " + e);
        await sleep(RETRY_DELAY_MS);
      }
    }
    wakeLoops.delete(sessionID);
  }

  return {
    "chat.message": async (input, output) => {
      const textParts = output.parts.filter((p) => p.type === "text");
      if (textParts.length === 0) return;
      const text = textParts.map((p) => p.text).join("");

      let result;
      try {
        result = await runChatPy(
          ["hook", "opencode", "relay"],
          JSON.stringify({ session_id: input.sessionID, prompt: text })
        );
      } catch (e) {
        await logError("relay call failed: " + e);
        return;
      }

      let decision;
      try {
        decision = JSON.parse(result.stdout);
      } catch (_e) {
        await logError("relay returned unparsable output: " + result.stdout);
        return;
      }
      if (decision.action !== "block") return;

      const replacement = decision.message && decision.message.startsWith("multi-agent-chat relay failed")
        ? "[multi-agent-chat] Relay failed: " + decision.message + ". Tell the human exactly this error in one line."
        : "[multi-agent-chat] (The human's message was posted to the group chat: " + decision.message +
          ". It is not addressed to you right now.) Reply with exactly: posted";

      const firstIdx = output.parts.indexOf(textParts[0]);
      output.parts[firstIdx].text = replacement;
      for (const p of textParts.slice(1)) {
        output.parts[output.parts.indexOf(p)].text = "";
      }
    },

    "tool.execute.after": async (input, output) => {
      if (input.tool !== "bash") return;
      const text = typeof output.output === "string" ? output.output : "";
      if (!text.includes("MAC_BIND")) return;
      const m = text.match(BIND_RE);
      if (!m) return;
      const [, chatId, name] = m;

      try {
        await runChatPy(
          ["hook", "opencode", "bind"],
          JSON.stringify({ session_id: input.sessionID, tool_response: text })
        );
      } catch (e) {
        await logError("bind call failed: " + e);
        return;
      }

      if (wakeLoops.has(input.sessionID)) return;
      wakeLoops.set(input.sessionID, true);
      wakeLoop(input.sessionID, chatId, name).catch((e) =>
        logError("wake loop crashed for " + chatId + "/" + name + ": " + e)
      );
    },
  };
};
