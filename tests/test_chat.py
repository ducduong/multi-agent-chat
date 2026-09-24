import concurrent.futures
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "skill", "multi-agent-chat", "scripts", "chat.py")
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")
PY = "/usr/bin/python3"

_spec = importlib.util.spec_from_file_location("chat", SCRIPT)
chat_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chat_module)


class ChatTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = dict(os.environ)
        self.env["MAC_HOME"] = self.home

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def write_config(self, **overrides):
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump(overrides, f)

    def db_path(self):
        return os.path.join(self.home, "data", "chat.db")

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db_path())
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def run_cmd(self, args, input_text=None, check=False):
        result = subprocess.run(
            [PY, SCRIPT] + args,
            input=input_text,
            capture_output=True,
            text=True,
            env=self.env,
        )
        if check and result.returncode != 0:
            self.fail(
                "command failed: %s\nrc=%d\nstdout=%s\nstderr=%s"
                % (args, result.returncode, result.stdout, result.stderr)
            )
        return result

    def create_chat(self, name="claude", topic="topic", max_turns=None, harness=None):
        args = ["create", "--name", name, "--topic", topic]
        if max_turns is not None:
            args += ["--max-turns", str(max_turns)]
        if harness:
            args += ["--harness", harness]
        r = self.run_cmd(args, check=True)
        return r.stdout.splitlines()[0]

    def join(self, chat_id, name, harness=None, check=True):
        args = ["join", chat_id, "--name", name]
        if harness:
            args += ["--harness", harness]
        return self.run_cmd(args, check=check)

    def start(self, chat_id, name, check=True):
        return self.run_cmd(["start", "--chat", chat_id, "--name", name], check=check)

    def post(self, chat_id, name, text):
        return self.run_cmd(["post", "--chat", chat_id, "--name", name, text])

    def wait(self, chat_id, name, timeout=2):
        return self.run_cmd(["wait", "--chat", chat_id, "--name", name, "--timeout", str(timeout)])

    def bind(self, harness, session_id, chat_id, name, as_object=False):
        if as_object:
            tool_response = {"stdout": "ok MAC_BIND chat=%s name=%s done" % (chat_id, name)}
        else:
            tool_response = "ok MAC_BIND chat=%s name=%s done" % (chat_id, name)
        payload = json.dumps({"session_id": session_id, "tool_response": tool_response})
        return self.run_cmd(["hook", harness, "bind"], input_text=payload, check=True)

    def relay(self, harness, session_id, prompt):
        payload = json.dumps({"session_id": session_id, "prompt": prompt})
        return self.run_cmd(["hook", harness, "relay"], input_text=payload)

    def setup_three_way_chat(self, max_turns=12):
        chat_id = self.create_chat(name="claude", topic="t", max_turns=max_turns)
        self.join(chat_id, "codex")
        self.join(chat_id, "opencode")
        self.start(chat_id, "claude")
        return chat_id


class TestLobbyFlow(ChatTestCase):
    def test_create_brief_join_start(self):
        chat_id = self.create_chat(name="claude", topic="split billing")
        r = self.run_cmd(
            ["brief", "--chat", chat_id, "--name", "claude", "--file", "-"],
            input_text="Goal: decide.\n",
            check=True,
        )
        self.assertEqual(r.stdout.strip(), "brief set")

        r = self.join(chat_id, "codex")
        self.assertIn("topic: split billing", r.stdout)
        self.assertIn("Goal: decide.", r.stdout)
        self.assertIn("MAC_BIND chat=%s name=codex" % chat_id, r.stdout)

        r = self.start(chat_id, "claude")
        self.assertIn("started", r.stdout)

        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("status: active", st.stdout)
        self.assertIn("speaker: claude", st.stdout)

    def test_only_host_can_start(self):
        chat_id = self.create_chat(name="claude", topic="t")
        self.join(chat_id, "codex")
        r = self.start(chat_id, "codex", check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_only_host_can_set_brief(self):
        chat_id = self.create_chat(name="claude", topic="t")
        self.join(chat_id, "codex")
        r = self.run_cmd(
            ["brief", "--chat", chat_id, "--name", "codex", "--file", "-"], input_text="x"
        )
        self.assertNotEqual(r.returncode, 0)

    def test_join_after_start_rejected(self):
        chat_id = self.create_chat(name="claude", topic="t")
        self.start(chat_id, "claude")
        r = self.join(chat_id, "codex", check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_duplicate_name_rejected(self):
        chat_id = self.create_chat(name="claude", topic="t")
        r = self.join(chat_id, "claude", check=False)
        self.assertNotEqual(r.returncode, 0)


class TestTail(ChatTestCase):
    def test_tail_on_lobby_chat_prints_header_line(self):
        chat_id = self.create_chat(name="claude", topic="split billing")
        self.join(chat_id, "codex")
        r = self.run_cmd(["tail", "--chat", chat_id], check=True)
        self.assertIn("chat %s · lobby · claude, codex · split billing" % chat_id, r.stdout)


class TestBrief(ChatTestCase):
    def test_brief_over_16kb_rejected(self):
        chat_id = self.create_chat(name="claude", topic="t")
        big = "x" * (16384 + 1)
        r = self.run_cmd(
            ["brief", "--chat", chat_id, "--name", "claude", "--file", "-"], input_text=big
        )
        self.assertNotEqual(r.returncode, 0)

    def test_brief_exactly_16kb_ok(self):
        chat_id = self.create_chat(name="claude", topic="t")
        ok = "x" * 16384
        r = self.run_cmd(
            ["brief", "--chat", chat_id, "--name", "claude", "--file", "-"], input_text=ok
        )
        self.assertEqual(r.returncode, 0)

    def test_brief_after_start_rejected(self):
        chat_id = self.create_chat(name="claude", topic="t")
        self.start(chat_id, "claude")
        r = self.run_cmd(
            ["brief", "--chat", chat_id, "--name", "claude", "--file", "-"], input_text="late"
        )
        self.assertNotEqual(r.returncode, 0)


class TestRotationAndTurns(ChatTestCase):
    def test_rotation_order(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("speaker: claude", st.stdout)

        self.post(chat_id, "claude", "a")
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("speaker: codex", st.stdout)

        self.post(chat_id, "codex", "b")
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("speaker: opencode", st.stdout)

        self.post(chat_id, "opencode", "c")
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("speaker: claude", st.stdout)

    def test_only_speaker_can_post_duplicate_rejected(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        r1 = self.post(chat_id, "claude", "a")
        self.assertEqual(r1.returncode, 0)
        r2 = self.post(chat_id, "claude", "again")
        self.assertEqual(r2.returncode, 2)
        self.assertIn("not your turn", r2.stderr)

    def test_max_turns_reached_via_posts_holds_for_host(self):
        chat_id = self.setup_three_way_chat(max_turns=2)
        self.post(chat_id, "claude", "a")
        r = self.post(chat_id, "codex", "b")
        self.assertIn("next: claude", r.stdout)
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("status: active", st.stdout)
        self.assertIn("speaker: claude", st.stdout)
        self.assertIn("turns: 2/2 (at limit", st.stdout)
        tail = self.run_cmd(["tail", "--chat", chat_id], check=True)
        self.assertIn("turn limit reached (2 turns)", tail.stdout)

        # only the host may act now; the next agent in rotation is rejected
        r2 = self.post(chat_id, "opencode", "c")
        self.assertEqual(r2.returncode, 2)
        self.assertIn("not your turn", r2.stderr)

    def test_explicit_end_command(self):
        chat_id = self.setup_three_way_chat()
        r = self.run_cmd(["end", "--chat", chat_id, "--name", "claude"], check=True)
        self.assertIn("ended", r.stdout)
        tail = self.run_cmd(["tail", "--chat", chat_id], check=True)
        self.assertIn("chat ended by claude", tail.stdout)


class TestTurnLimit(ChatTestCase):
    def reach_limit(self, max_turns=2):
        chat_id = self.setup_three_way_chat(max_turns=max_turns)
        self.post(chat_id, "claude", "a")
        self.post(chat_id, "codex", "b")  # turns_taken == max_turns; speaker becomes host (claude)
        return chat_id

    def test_host_wait_at_limit_shows_limit_header(self):
        chat_id = self.reach_limit()
        r = self.wait(chat_id, "claude", timeout=2)
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"your turn \(\d+/\d+\)")
        self.assertIn("turn limit reached", r.stdout)

    def test_host_post_at_limit_ends_chat(self):
        chat_id = self.reach_limit()
        r = self.post(chat_id, "claude", "closing summary")
        self.assertEqual(r.returncode, 0)
        self.assertIn("chat ended", r.stdout)
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("status: ended", st.stdout)
        tail = self.run_cmd(["tail", "--chat", chat_id], check=True)
        self.assertIn("chat closed by claude", tail.stdout)

        r2 = self.wait(chat_id, "codex", timeout=2)
        self.assertEqual(r2.returncode, 3)


class TestExtend(ChatTestCase):
    def test_non_host_extend_rejected(self):
        chat_id = self.setup_three_way_chat(max_turns=2)
        r = self.run_cmd(["extend", "--chat", chat_id, "--name", "codex", "--turns", "3"])
        self.assertNotEqual(r.returncode, 0)

    def test_extend_requires_positive_turns(self):
        chat_id = self.setup_three_way_chat(max_turns=2)
        r = self.run_cmd(["extend", "--chat", chat_id, "--name", "claude", "--turns", "0"])
        self.assertNotEqual(r.returncode, 0)

    def test_host_extend_at_limit_then_post_advances_rotation(self):
        chat_id = self.setup_three_way_chat(max_turns=2)
        self.post(chat_id, "claude", "a")
        self.post(chat_id, "codex", "b")  # at limit; speaker=claude (host)

        r = self.run_cmd(
            ["extend", "--chat", chat_id, "--name", "claude", "--turns", "3"], check=True
        )
        self.assertIn("extended", r.stdout)
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("speaker: claude", st.stdout)
        self.assertIn("turns: 2/5", st.stdout)
        tail = self.run_cmd(["tail", "--chat", chat_id], check=True)
        self.assertIn("claude extended the chat by 3 turns (now 5)", tail.stdout)

        r2 = self.post(chat_id, "claude", "continuing")
        self.assertEqual(r2.returncode, 0)
        self.assertIn("next: codex", r2.stdout)  # rotation resumes normally after the host's turn


class TestTimeout(ChatTestCase):
    def test_timeout_skip_counts_turn_and_does_not_advance_read_seq(self):
        self.write_config(max_turns=12, turn_timeout_s=1, wait_timeout_s=5)
        chat_id = self.setup_three_way_chat()
        import time

        time.sleep(1.5)
        # any command triggers the lazy timeout check
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("speaker: codex", st.stdout)
        self.assertIn("turns: 1/12", st.stdout)

        tail = self.run_cmd(["tail", "--chat", chat_id], check=True)
        self.assertIn("skipped claude (no reply in 1s)", tail.stdout)

        rows = self.query(
            "SELECT read_seq FROM participants WHERE chat_id=? AND name='claude'", (chat_id,)
        )
        self.assertEqual(rows[0][0], 0)

    def test_timeout_skip_reaching_limit_then_no_further_skip(self):
        self.write_config(max_turns=2, turn_timeout_s=1, wait_timeout_s=5)
        chat_id = self.setup_three_way_chat(max_turns=2)
        self.post(chat_id, "claude", "a")  # turns_taken=1, speaker=codex

        time.sleep(1.5)
        st = self.run_cmd(["status", "--chat", chat_id], check=True)  # skip pushes turns_taken to the limit
        self.assertIn("status: active", st.stdout)
        self.assertIn("speaker: claude", st.stdout)
        self.assertIn("turns: 2/2 (at limit", st.stdout)

        time.sleep(1.5)  # would be another overdue turn if the timeout still applied
        st2 = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("speaker: claude", st2.stdout)
        self.assertIn("turns: 2/2 (at limit", st2.stdout)
        tail = self.run_cmd(["tail", "--chat", chat_id], check=True)
        self.assertEqual(tail.stdout.count("skipped"), 1)

    def test_wait_exit_codes(self):
        self.write_config(max_turns=12, turn_timeout_s=600, wait_timeout_s=5)
        chat_id = self.setup_three_way_chat()

        r = self.wait(chat_id, "claude", timeout=2)
        self.assertEqual(r.returncode, 0)
        self.assertIn("your turn", r.stdout)

        r = self.wait(chat_id, "codex", timeout=1)
        self.assertEqual(r.returncode, 4)

        self.run_cmd(["end", "--chat", chat_id, "--name", "claude"], check=True)
        r = self.wait(chat_id, "codex", timeout=2)
        self.assertEqual(r.returncode, 3)


class TestDelivery(ChatTestCase):
    def test_message_after_wait_delivered_next_turn(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        session_id = "sess-1"
        self.bind("claude", session_id, chat_id, "claude")

        r = self.wait(chat_id, "claude", timeout=1)
        self.assertEqual(r.returncode, 0)

        # arrives after claude's shown_seq was frozen, before claude posts
        r = self.relay("claude", session_id, "note while claude is speaking")
        self.assertEqual(r.returncode, 0)
        self.assertIn("posted", json.loads(r.stdout)["reason"])

        self.post(chat_id, "claude", "a")
        self.post(chat_id, "codex", "b")
        self.post(chat_id, "opencode", "c")

        r = self.wait(chat_id, "claude", timeout=1)
        self.assertEqual(r.returncode, 0)
        self.assertIn("note while claude is speaking", r.stdout)


class TestMentions(ChatTestCase):
    def test_mention_does_not_interrupt_and_picks_next(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        session_id = "sess-1"
        self.bind("claude", session_id, chat_id, "claude")

        r = self.relay("claude", session_id, "@opencode what do you think?")
        self.assertEqual(r.returncode, 0)

        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("speaker: claude", st.stdout)  # current turn undisturbed

        self.post(chat_id, "claude", "a")
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("speaker: opencode", st.stdout)  # mention wins over rotation

        # rotation continues from the mentioned speaker's position
        self.post(chat_id, "opencode", "b")
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("speaker: claude", st.stdout)

    def test_last_mention_wins(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        session_id = "sess-1"
        self.bind("claude", session_id, chat_id, "claude")

        self.relay("claude", session_id, "@codex or actually @opencode go")

        self.post(chat_id, "claude", "a")
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("speaker: opencode", st.stdout)


class TestRelayDecisions(ChatTestCase):
    def test_unbound_session_passes_silently(self):
        chat_id = self.create_chat(name="claude", topic="t")
        r = self.relay("claude", "never-bound", "hello")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_no_db_file_passes_silently(self):
        r = self.relay("claude", "whatever", "hello")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertFalse(os.path.exists(self.db_path()))

    def test_bracket_prefixed_prompt_passes(self):
        chat_id = self.setup_three_way_chat()
        self.bind("claude", "s1", chat_id, "claude")
        r = self.relay("claude", "s1", "  [multi-agent-chat] injected content")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_slash_prefixed_prompt_passes(self):
        chat_id = self.setup_three_way_chat()
        self.bind("claude", "s1", chat_id, "claude")
        r = self.relay("claude", "s1", "// summarize for me")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_task_notification_passes(self):
        chat_id = self.setup_three_way_chat()
        self.bind("claude", "s1", chat_id, "claude")
        r = self.relay("claude", "s1", "<task-notification>\n<status>completed</status>\n</task-notification>")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_lobby_prompt_passes_through(self):
        chat_id = self.create_chat(name="claude", topic="t")
        self.bind("claude", "s1", chat_id, "claude")
        r = self.relay("claude", "s1", "everyone joined, start the chat")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        rows = self.query("SELECT * FROM messages WHERE chat_id=?", (chat_id,))
        self.assertEqual(rows, [])

    def test_public_prompt_blocks_and_posts(self):
        chat_id = self.setup_three_way_chat()
        self.bind("claude", "s1", chat_id, "claude")
        r = self.relay("claude", "s1", "what about latency?")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertEqual(data["decision"], "block")
        self.assertIn("posted to chat %s as #2" % chat_id, data["reason"])
        rows = self.query(
            "SELECT sender, kind, via, text FROM messages WHERE chat_id=? AND kind='human'", (chat_id,)
        )
        self.assertEqual(rows, [("human", "human", "claude", "what about latency?")])

    def test_mention_prompt_blocks_and_sets_next_speaker(self):
        chat_id = self.setup_three_way_chat()
        self.bind("claude", "s1", chat_id, "claude")
        r = self.relay("claude", "s1", "@codex your take?")
        data = json.loads(r.stdout)
        self.assertEqual(data["decision"], "block")
        rows = self.query("SELECT next_speaker FROM chats WHERE id=?", (chat_id,))
        self.assertEqual(rows[0][0], "codex")

    def test_relay_failure_blocks_with_error(self):
        chat_id = self.create_chat(name="claude", topic="t")
        self.bind("claude", "s1", chat_id, "claude")
        conn = sqlite3.connect(self.db_path())
        conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        conn.commit()
        conn.close()
        r = self.relay("claude", "s1", "hello")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertEqual(data["decision"], "block")
        self.assertTrue(data["reason"].startswith("multi-agent-chat relay failed:"))
        self.assertIn("NOT posted", data["reason"])

    def test_opencode_relay_output_shape(self):
        chat_id = self.setup_three_way_chat()
        self.bind("opencode", "s1", chat_id, "claude")

        r = self.relay("opencode", "never-bound-oc", "hello")
        self.assertEqual(json.loads(r.stdout), {"action": "pass", "message": ""})

        r = self.relay("opencode", "s1", "public message")
        data = json.loads(r.stdout)
        self.assertEqual(data["action"], "block")
        self.assertIn("posted to chat", data["message"])

        r = self.relay("opencode", "s1", "// private")
        self.assertEqual(json.loads(r.stdout), {"action": "pass", "message": ""})


class TestBind(ChatTestCase):
    def test_bind_from_string_tool_response(self):
        chat_id = self.create_chat(name="claude", topic="t")
        self.bind("claude", "sess-a", chat_id, "claude", as_object=False)
        rows = self.query(
            "SELECT chat_id, name FROM sessions WHERE harness='claude' AND session_id='sess-a'"
        )
        self.assertEqual(rows, [(chat_id, "claude")])

    def test_bind_from_object_tool_response(self):
        chat_id = self.create_chat(name="claude", topic="t")
        self.bind("claude", "sess-b", chat_id, "claude", as_object=True)
        rows = self.query(
            "SELECT chat_id, name FROM sessions WHERE harness='claude' AND session_id='sess-b'"
        )
        self.assertEqual(rows, [(chat_id, "claude")])

    def test_bind_no_match_is_noop(self):
        payload = json.dumps({"session_id": "sess-c", "tool_response": "nothing here"})
        r = self.run_cmd(["hook", "claude", "bind"], input_text=payload, check=True)
        self.assertEqual(r.stdout, "")
        rows = self.query("SELECT * FROM sessions") if os.path.exists(self.db_path()) else []
        self.assertEqual(rows, [])

    def test_bind_opencode_output_shape(self):
        chat_id = self.create_chat(name="claude", topic="t")
        r = self.bind("opencode", "sess-d", chat_id, "claude")
        self.assertEqual(json.loads(r.stdout), {"action": "pass"})


class TestConcurrency(ChatTestCase):
    def test_concurrent_human_relays_gap_free_seq(self):
        chat_id = self.create_chat(name="claude", topic="t")
        self.start(chat_id, "claude")
        self.bind("claude", "concurrent-sess", chat_id, "claude")

        n_per_worker = 30
        n_workers = 3
        total = n_per_worker * n_workers

        def post_one(i):
            r = self.relay("claude", "concurrent-sess", "msg %d" % i)
            data = json.loads(r.stdout)
            m = re.search(r"as #(\d+)", data["reason"])
            return int(m.group(1))

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
            seqs = list(pool.map(post_one, range(total)))

        # seq 1 is the "chat started" system message from start(); human messages follow
        self.assertEqual(sorted(seqs), list(range(2, total + 2)))
        rows = self.query("SELECT seq FROM messages WHERE chat_id=? ORDER BY seq", (chat_id,))
        self.assertEqual([row[0] for row in rows], list(range(1, total + 2)))


class TestNameValidation(ChatTestCase):
    def test_create_rejects_invalid_names(self):
        for bad in ["Claude", "cla ude", "-claude", "claude-", "", "x" * 33]:
            r = self.run_cmd(["create", "--name", bad, "--topic", "t"])
            self.assertNotEqual(r.returncode, 0, bad)

    def test_join_rejects_invalid_name(self):
        chat_id = self.create_chat(name="claude", topic="t")
        r = self.join(chat_id, "Codex", check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_valid_names_accepted(self):
        chat_id = self.create_chat(name="agent_1", topic="t")
        r = self.join(chat_id, "abc123", check=False)
        self.assertEqual(r.returncode, 0)
        r = self.join(chat_id, "claude-fable", check=False)
        self.assertEqual(r.returncode, 0)

    def test_hyphenated_names_bind_and_mention(self):
        chat_id = self.create_chat(name="claude-opus", topic="t")
        self.join(chat_id, "claude-fable")
        self.join(chat_id, "codex-sol")
        self.start(chat_id, "claude-opus")
        self.bind("claude", "s1", chat_id, "claude-fable", as_object=True)
        rows = self.query("SELECT name FROM sessions WHERE session_id='s1'")
        self.assertEqual(rows[0][0], "claude-fable")
        self.relay("claude", "s1", "thoughts, @Codex-Sol?")
        rows = self.query("SELECT next_speaker FROM chats WHERE id=?", (chat_id,))
        self.assertEqual(rows[0][0], "codex-sol")


class TestSkipTurn(ChatTestCase):
    def test_skip_turn_waits_past_the_named_turn(self):
        chat_id = self.setup_three_way_chat()
        r = self.run_cmd(
            ["wait", "--chat", chat_id, "--name", "claude", "--timeout", "1", "--skip-turn", "1"]
        )
        self.assertEqual(r.returncode, 4)

        r = self.run_cmd(["wait", "--chat", chat_id, "--name", "claude", "--timeout", "2"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("your turn (1/12)", r.stdout)

    def test_skip_turn_returns_once_a_different_turn_is_ours(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        self.post(chat_id, "claude", "a")
        self.post(chat_id, "codex", "b")
        self.post(chat_id, "opencode", "c")

        r = self.run_cmd(
            ["wait", "--chat", chat_id, "--name", "claude", "--timeout", "2", "--skip-turn", "1"]
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn("your turn (4/12)", r.stdout)

    def test_skip_turn_does_not_freeze_delivery(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        session_id = "s1"
        self.bind("claude", session_id, chat_id, "claude")

        r = self.run_cmd(
            ["wait", "--chat", chat_id, "--name", "claude", "--timeout", "1", "--skip-turn", "1"]
        )
        self.assertEqual(r.returncode, 4)

        self.relay("claude", session_id, "hello during skip")

        r = self.run_cmd(["wait", "--chat", chat_id, "--name", "claude", "--timeout", "2"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("hello during skip", r.stdout)

    def test_skip_turn_ended_chat_still_reported(self):
        chat_id = self.setup_three_way_chat()
        self.run_cmd(["end", "--chat", chat_id, "--name", "claude"], check=True)
        r = self.run_cmd(
            ["wait", "--chat", chat_id, "--name", "claude", "--timeout", "2", "--skip-turn", "1"]
        )
        self.assertEqual(r.returncode, 3)


class TestWrappingQuotes(ChatTestCase):
    def test_wrapping_quotes_stripped_before_posting(self):
        chat_id = self.setup_three_way_chat()
        self.bind("claude", "s1", chat_id, "claude")
        r = self.relay("claude", "s1", '"what about latency?"')
        data = json.loads(r.stdout)
        self.assertEqual(data["decision"], "block")
        rows = self.query("SELECT text FROM messages WHERE chat_id=? AND kind='human'", (chat_id,))
        self.assertEqual(rows, [("what about latency?",)])

    def test_wrapping_quotes_stripped_before_prefix_check(self):
        chat_id = self.setup_three_way_chat()
        self.bind("claude", "s1", chat_id, "claude")
        r = self.relay("claude", "s1", '"[multi-agent-chat] injected"')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_single_quote_not_stripped(self):
        chat_id = self.setup_three_way_chat()
        self.bind("claude", "s1", chat_id, "claude")
        r = self.relay("claude", "s1", '"unbalanced')
        data = json.loads(r.stdout)
        rows = self.query("SELECT text FROM messages WHERE chat_id=? AND kind='human'", (chat_id,))
        self.assertEqual(rows, [('"unbalanced',)])


class TestCodexWaker(ChatTestCase):
    def setUp(self):
        super().setUp()
        self.codex_log = os.path.join(self.home, "fake-codex.log")
        self.codex_bin = os.path.join(self.home, "fake-codex.sh")
        self.set_fake_codex(exit_code=0)
        self.env["MAC_CODEX_BIN"] = self.codex_bin

    def set_fake_codex(self, exit_code):
        with open(self.codex_bin, "w") as f:
            f.write('#!/bin/sh\necho "$@" >> "%s"\nexit %d\n' % (self.codex_log, exit_code))
        os.chmod(self.codex_bin, 0o755)

    def waker_pidfile(self, chat_id, name):
        return os.path.join(self.home, "data", "wakers", "%s-%s.pid" % (chat_id, name))

    def wait_until(self, predicate, timeout, message):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail(message)

    def codex_log_content(self):
        if not os.path.exists(self.codex_log):
            return ""
        with open(self.codex_log) as f:
            return f.read()

    def count_invocations(self):
        # the turn text spans multiple lines, so count call preambles, not lines
        return self.codex_log_content().count("queue --thread")

    def test_bind_spawns_exactly_one_waker_delivers_turn_and_exits_on_end(self):
        chat_id = self.create_chat(name="codex", topic="t", max_turns=2)
        self.join(chat_id, "claude")
        self.start(chat_id, "codex")
        session_id = "codex-sess-1"
        self.bind("codex", session_id, chat_id, "codex")

        pidfile = self.waker_pidfile(chat_id, "codex")
        self.wait_until(lambda: os.path.exists(pidfile), 5, "waker pidfile never appeared")
        with open(pidfile) as f:
            pid1 = f.read().strip()

        self.wait_until(lambda: self.count_invocations() >= 1, 5, "codex queue never called")
        content = self.codex_log_content()
        self.assertIn("--thread", content)
        self.assertIn(session_id, content)
        self.assertIn("your turn (1/2)", content)

        # a second bind (e.g. a later PostToolUse) must not spawn a second waker
        self.bind("codex", session_id, chat_id, "codex")
        with open(pidfile) as f:
            pid2 = f.read().strip()
        self.assertEqual(pid1, pid2)

        self.post(chat_id, "codex", "a")
        self.post(chat_id, "claude", "b")  # reaches the limit; speaker becomes codex (host)

        # the waker delivers the at-limit turn to the host exactly once
        self.wait_until(lambda: self.count_invocations() >= 2, 5, "limit turn never queued")
        content = self.codex_log_content()
        self.assertIn("turn limit reached", content)
        self.assertEqual(self.count_invocations(), 2)

        # host decides to extend rather than close; rotation then resumes normally
        self.run_cmd(["extend", "--chat", chat_id, "--name", "codex", "--turns", "2"], check=True)
        self.post(chat_id, "codex", "c")

        r = self.wait(chat_id, "claude", timeout=2)
        self.assertEqual(r.returncode, 0)
        self.assertIn("your turn", r.stdout)

        self.run_cmd(["end", "--chat", chat_id, "--name", "codex"], check=True)
        self.wait_until(lambda: not os.path.exists(pidfile), 8, "waker did not exit after chat ended")
        self.assertEqual(self.count_invocations(), 2)  # limit turn queued exactly once, never re-delivered

    def test_queue_failure_is_logged_and_not_marked_delivered(self):
        self.set_fake_codex(exit_code=1)
        chat_id = self.create_chat(name="codex", topic="t", max_turns=12)
        self.join(chat_id, "claude")
        self.start(chat_id, "codex")
        session_id = "codex-sess-2"
        self.bind("codex", session_id, chat_id, "codex")

        pidfile = self.waker_pidfile(chat_id, "codex")
        self.wait_until(lambda: os.path.exists(pidfile), 5, "waker pidfile never appeared")
        waker_log = os.path.join(self.home, "data", "wakers", "%s-codex.log" % chat_id)
        self.wait_until(
            lambda: os.path.exists(waker_log) and os.path.getsize(waker_log) > 0,
            5,
            "waker did not log the queue failure",
        )
        with open(waker_log) as f:
            content = f.read()
        self.assertIn("codex queue failed", content)
        self.assertTrue(os.path.exists(pidfile))  # still running, retrying

        self.run_cmd(["end", "--chat", chat_id, "--name", "codex"], check=True)
        self.wait_until(lambda: not os.path.exists(pidfile), 8, "waker did not exit after chat ended")


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = dict(os.environ)
        self.env["HOME"] = self.home
        self.env.pop("MAC_HOME", None)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def seed(self):
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(self.home, ".codex"), exist_ok=True)
        claude_settings = {
            "model": "sonnet",
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "/bin/echo unrelated"}]}
                ]
            },
        }
        codex_hooks = {
            "description": "codex hooks",
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "/bin/echo session-start"}]}],
                "PostToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "/bin/echo audor-hook"}]}
                ],
            },
        }
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as f:
            json.dump(claude_settings, f)
        with open(os.path.join(self.home, ".codex", "hooks.json"), "w") as f:
            json.dump(codex_hooks, f)
        return claude_settings, codex_hooks

    def run_install(self, *args):
        return subprocess.run(
            ["bash", INSTALL_SH] + list(args), env=self.env, capture_output=True, text=True
        )

    def load(self, *parts):
        with open(os.path.join(self.home, *parts)) as f:
            return json.load(f)

    def count_mac_groups(self, groups):
        return sum(
            1 for g in groups if any("multi-agent-chat" in h.get("command", "") for h in g.get("hooks", []))
        )

    def symlink_paths(self):
        return [
            os.path.join(self.home, ".claude", "skills", "multi-agent-chat"),
            os.path.join(self.home, ".codex", "skills", "multi-agent-chat"),
            os.path.join(self.home, ".config", "opencode", "skills", "multi-agent-chat"),
            os.path.join(self.home, ".config", "opencode", "plugins", "multi-agent-chat.js"),
        ]

    def test_install_twice_leaves_one_set_of_entries_and_preserves_unrelated_content(self):
        self.seed()
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)

        claude = self.load(".claude", "settings.json")
        self.assertEqual(claude["model"], "sonnet")
        ups = claude["hooks"]["UserPromptSubmit"]
        self.assertEqual(self.count_mac_groups(ups), 1)
        self.assertTrue(any("unrelated" in h["command"] for g in ups for h in g["hooks"]))
        self.assertEqual(self.count_mac_groups(claude["hooks"]["PostToolUse"]), 1)

        codex = self.load(".codex", "hooks.json")
        self.assertEqual(codex["description"], "codex hooks")
        self.assertEqual(sorted(codex.keys()), ["description", "hooks"])
        self.assertEqual(
            codex["hooks"]["SessionStart"],
            [{"hooks": [{"type": "command", "command": "/bin/echo session-start"}]}],
        )
        self.assertEqual(self.count_mac_groups(codex["hooks"]["PostToolUse"]), 1)
        self.assertTrue(
            any("audor-hook" in h["command"] for g in codex["hooks"]["PostToolUse"] for h in g["hooks"])
        )
        self.assertEqual(self.count_mac_groups(codex["hooks"]["UserPromptSubmit"]), 1)

        for link in self.symlink_paths():
            self.assertTrue(os.path.islink(link), link)

    def test_config_json_written_with_chat_py_defaults(self):
        self.run_install()
        cfg = self.load(".multi-agent-chat", "config.json")
        self.assertEqual(cfg, chat_module.DEFAULT_CONFIG)

    def test_uninstall_restores_originals_and_removes_symlinks(self):
        claude_orig, codex_orig = self.seed()
        self.run_install()
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)

        self.assertEqual(self.load(".claude", "settings.json"), claude_orig)
        self.assertEqual(self.load(".codex", "hooks.json"), codex_orig)

        for link in self.symlink_paths():
            self.assertFalse(os.path.islink(link) or os.path.exists(link), link)

        self.assertTrue(os.path.isdir(os.path.join(self.home, ".multi-agent-chat", "backup")))

    def test_install_aborts_when_target_is_not_a_symlink(self):
        os.makedirs(os.path.join(self.home, ".claude", "skills"), exist_ok=True)
        with open(os.path.join(self.home, ".claude", "skills", "multi-agent-chat"), "w") as f:
            f.write("not a symlink")
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
