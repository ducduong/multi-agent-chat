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

    def join(self, chat_id, name, harness=None, check=True, rejoin=False, latest=False):
        args = ["join"]
        if not latest:
            args.append(chat_id)
        args += ["--name", name]
        if harness:
            args += ["--harness", harness]
        if rejoin:
            args.append("--rejoin")
        if latest:
            args.append("--latest")
        return self.run_cmd(args, check=check)

    def start(self, chat_id, name, check=True):
        return self.run_cmd(["start", "--chat", chat_id, "--name", name], check=check)

    def post(self, chat_id, name, text):
        return self.run_cmd(["post", "--chat", chat_id, "--name", name, text])

    def pass_turn(self, chat_id, name):
        return self.run_cmd(["pass", "--chat", chat_id, "--name", name])

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

    def bind_direct(self, harness, session_id, chat_id, name):
        """Insert a sessions row without going through hook_bind, so tests that only
        need a bound session don't trigger hook_bind's codex-waker side effect."""
        conn = sqlite3.connect(self.db_path())
        conn.execute(
            "INSERT INTO sessions (harness, session_id, chat_id, name) VALUES (?,?,?,?) "
            "ON CONFLICT(harness, session_id) DO UPDATE SET chat_id=excluded.chat_id, "
            "name=excluded.name",
            (harness, session_id, chat_id, name),
        )
        conn.commit()
        conn.close()

    def stop_hook(self, harness, session_id, stop_hook_active=False):
        payload = json.dumps({"session_id": session_id, "stop_hook_active": stop_hook_active})
        return self.run_cmd(["hook", harness, "stop"], input_text=payload)

    def set_wait_pid(self, chat_id, name, pid):
        conn = sqlite3.connect(self.db_path())
        conn.execute(
            "UPDATE participants SET wait_pid=? WHERE chat_id=? AND name=?", (pid, chat_id, name)
        )
        conn.commit()
        conn.close()

    def reopen(self, chat_id, name, turns=None, check=True):
        args = ["reopen", "--chat", chat_id, "--name", name]
        if turns is not None:
            args += ["--turns", str(turns)]
        return self.run_cmd(args, check=check)

    def setup_three_way_chat(self, max_turns=12):
        chat_id = self.create_chat(name="claude", topic="t", max_turns=max_turns)
        self.join(chat_id, "codex")
        self.join(chat_id, "opencode")
        self.start(chat_id, "claude")
        return chat_id

    def setup_two_way_chat(self, max_turns=12):
        chat_id = self.create_chat(name="claude", topic="t", max_turns=max_turns)
        self.join(chat_id, "codex")
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
    def test_non_participant_extend_rejected(self):
        chat_id = self.setup_three_way_chat(max_turns=2)
        r = self.run_cmd(["extend", "--chat", chat_id, "--name", "nobody", "--turns", "3"])
        self.assertNotEqual(r.returncode, 0)

    def test_extend_requires_positive_turns(self):
        chat_id = self.setup_three_way_chat(max_turns=2)
        r = self.run_cmd(["extend", "--chat", chat_id, "--name", "claude", "--turns", "0"])
        self.assertNotEqual(r.returncode, 0)

    def test_non_host_participant_extend_mid_chat_leaves_speaker_and_clock_unchanged(self):
        chat_id = self.setup_three_way_chat(max_turns=12)  # not at limit; speaker=claude (host)
        before = self.query(
            "SELECT speaker, turn_started_at, max_turns FROM chats WHERE id=?", (chat_id,)
        )[0]

        r = self.run_cmd(
            ["extend", "--chat", chat_id, "--name", "codex", "--turns", "3"], check=True
        )
        self.assertIn("extended", r.stdout)

        after = self.query(
            "SELECT speaker, turn_started_at, max_turns FROM chats WHERE id=?", (chat_id,)
        )[0]
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1], before[1])
        self.assertEqual(after[2], before[2] + 3)
        tail = self.run_cmd(["tail", "--chat", chat_id], check=True)
        self.assertIn("codex extended the chat by 3 turns (now %d)" % after[2], tail.stdout)

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


class TestPass(ChatTestCase):
    def test_pass_stores_no_message_and_advances_rotation(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        before = self.query("SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,))[0][0]

        r = self.pass_turn(chat_id, "claude")
        self.assertEqual(r.returncode, 0)
        self.assertIn("passed · next: codex", r.stdout)

        after = self.query("SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,))[0][0]
        self.assertEqual(after, before)
        row = self.query("SELECT turns_taken, speaker FROM chats WHERE id=?", (chat_id,))[0]
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], "codex")

    def test_pass_advances_read_seq_to_shown_seq(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        self.wait(chat_id, "claude", timeout=1)  # sets shown_seq to the current max
        self.pass_turn(chat_id, "claude")
        rows = self.query(
            "SELECT shown_seq, read_seq FROM participants WHERE chat_id=? AND name='claude'",
            (chat_id,),
        )
        self.assertEqual(rows[0][0], rows[0][1])

    def test_non_speaker_pass_rejected(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        r = self.pass_turn(chat_id, "codex")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not your turn", r.stderr)

    def test_pass_at_limit_rejected(self):
        chat_id = self.setup_three_way_chat(max_turns=2)
        self.post(chat_id, "claude", "a")
        self.post(chat_id, "codex", "b")  # at limit; speaker=claude (host)
        r = self.pass_turn(chat_id, "claude")
        self.assertEqual(r.returncode, 2)
        self.assertIn("turn limit reached", r.stderr)


class TestPassCompat(ChatTestCase):
    def test_short_pass_prefixed_post_behaves_as_pass(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        r = self.post(chat_id, "claude", "pass — nothing to add")
        self.assertEqual(r.returncode, 0)
        self.assertIn("passed · next: codex", r.stdout)
        rows = self.query(
            "SELECT COUNT(*) FROM messages WHERE chat_id=? AND kind='agent'", (chat_id,)
        )
        self.assertEqual(rows[0][0], 0)

    def test_long_pass_prefixed_post_is_a_real_post(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        text = "pass the config through the env and update the readme accordingly please"
        self.assertGreater(len(text), 60)
        r = self.post(chat_id, "claude", text)
        self.assertEqual(r.returncode, 0)
        self.assertIn("posted #", r.stdout)
        rows = self.query(
            "SELECT text FROM messages WHERE chat_id=? AND kind='agent'", (chat_id,)
        )
        self.assertEqual(rows, [(text,)])

    def test_real_post_resets_passes(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        self.pass_turn(chat_id, "claude")  # passes=1, speaker=codex
        self.post(chat_id, "codex", "hello")  # real post resets passes
        row = self.query("SELECT passes FROM chats WHERE id=?", (chat_id,))[0]
        self.assertEqual(row[0], 0)


class TestPauseFlow(ChatTestCase):
    def test_two_consecutive_passes_pause_with_a_single_system_message(self):
        chat_id = self.setup_two_way_chat(max_turns=12)
        self.pass_turn(chat_id, "claude")
        r = self.pass_turn(chat_id, "codex")
        self.assertEqual(r.returncode, 0)
        self.assertIn("passed · chat paused until the human replies", r.stdout)

        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("status: paused (waiting for the human)", st.stdout)
        tail = self.run_cmd(["tail", "--chat", chat_id], check=True)
        self.assertEqual(tail.stdout.count("everyone passed"), 1)

    def test_wait_times_out_for_either_agent_while_paused(self):
        chat_id = self.setup_two_way_chat(max_turns=12)
        self.pass_turn(chat_id, "claude")
        self.pass_turn(chat_id, "codex")
        r1 = self.wait(chat_id, "claude", timeout=1)
        self.assertEqual(r1.returncode, 4)
        r2 = self.wait(chat_id, "codex", timeout=1)
        self.assertEqual(r2.returncode, 4)


class TestPauseResume(ChatTestCase):
    def pause_three_way(self, chat_id):
        self.pass_turn(chat_id, "claude")
        self.pass_turn(chat_id, "codex")
        self.pass_turn(chat_id, "opencode")  # 3 passes == 3 participants -> paused, speaker=claude

    def test_human_relay_while_paused_resumes_next_in_line_speaker(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        self.pause_three_way(chat_id)

        self.bind("claude", "s1", chat_id, "claude")
        r = self.relay("claude", "s1", "let's continue")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertEqual(data["decision"], "block")
        self.assertIn("posted to chat", data["reason"])

        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("status: active", st.stdout)
        self.assertIn("speaker: claude", st.stdout)  # next-in-line after opencode's pass

        row = self.query("SELECT passes FROM chats WHERE id=?", (chat_id,))[0]
        self.assertEqual(row[0], 0)

        w = self.wait(chat_id, "claude", timeout=2)
        self.assertEqual(w.returncode, 0)
        self.assertIn("let's continue", w.stdout)

    def test_human_relay_with_mention_resumes_mentioned_agent(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        self.pause_three_way(chat_id)

        self.bind("claude", "s1", chat_id, "claude")
        self.relay("claude", "s1", "@codex please take over")

        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("status: active", st.stdout)
        self.assertIn("speaker: codex", st.stdout)

    def test_human_message_while_active_resets_passes(self):
        chat_id = self.setup_two_way_chat(max_turns=12)
        self.pass_turn(chat_id, "claude")  # passes=1, speaker=codex

        self.bind("claude", "s1", chat_id, "claude")
        self.relay("claude", "s1", "just a note")  # resets passes while still active

        row = self.query("SELECT passes FROM chats WHERE id=?", (chat_id,))[0]
        self.assertEqual(row[0], 0)

        r = self.pass_turn(chat_id, "codex")  # passes=1 again, not the full round
        self.assertEqual(r.returncode, 0)
        self.assertIn("passed · next: claude", r.stdout)
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("status: active", st.stdout)


class TestPausedControls(ChatTestCase):
    def pause_three_way(self, chat_id):
        self.pass_turn(chat_id, "claude")
        self.pass_turn(chat_id, "codex")
        self.pass_turn(chat_id, "opencode")

    def test_extend_works_while_paused(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        self.pause_three_way(chat_id)
        r = self.run_cmd(
            ["extend", "--chat", chat_id, "--name", "claude", "--turns", "3"], check=True
        )
        self.assertIn("extended", r.stdout)
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("status: paused", st.stdout)

    def test_end_works_while_paused(self):
        chat_id = self.setup_three_way_chat(max_turns=12)
        self.pause_three_way(chat_id)
        r = self.run_cmd(["end", "--chat", chat_id, "--name", "claude"], check=True)
        self.assertIn("ended", r.stdout)
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("status: ended", st.stdout)


class TestTimeout(ChatTestCase):
    def test_timeout_skip_does_not_count_turn_and_does_not_advance_read_seq(self):
        self.write_config(max_turns=12, turn_timeout_s=1, wait_timeout_s=5)
        chat_id = self.setup_three_way_chat()
        import time

        time.sleep(1.5)
        # any command triggers the lazy timeout check
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("speaker: codex", st.stdout)
        self.assertIn("turns: 0/12", st.stdout)  # a skip is a free pass, not a turn

        tail = self.run_cmd(["tail", "--chat", chat_id], check=True)
        self.assertIn("skipped claude (no reply in 1s)", tail.stdout)

        rows = self.query(
            "SELECT read_seq FROM participants WHERE chat_id=? AND name='claude'", (chat_id,)
        )
        self.assertEqual(rows[0][0], 0)

        row = self.query("SELECT passes FROM chats WHERE id=?", (chat_id,))[0]
        self.assertEqual(row[0], 1)

    def test_repeated_skips_never_reach_turn_limit_but_eventually_pause(self):
        self.write_config(max_turns=1, turn_timeout_s=1, wait_timeout_s=5)
        chat_id = self.setup_three_way_chat(max_turns=1)

        time.sleep(1.5)  # skip 1/3: claude -> codex
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("turns: 0/1", st.stdout)

        time.sleep(1.5)  # skip 2/3: codex -> opencode
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("turns: 0/1", st.stdout)

        time.sleep(1.5)  # skip 3/3 == participant count -> paused, never the turn limit
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("status: paused", st.stdout)

        tail = self.run_cmd(["tail", "--chat", chat_id], check=True)
        self.assertEqual(tail.stdout.count("skipped"), 3)
        self.assertIn("everyone passed", tail.stdout)

    def test_skip_plus_pass_in_two_agent_chat_pauses(self):
        self.write_config(max_turns=12, turn_timeout_s=1, wait_timeout_s=5)
        chat_id = self.setup_two_way_chat()

        time.sleep(1.5)  # skip: claude -> codex, passes=1
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("speaker: codex", st.stdout)

        r = self.pass_turn(chat_id, "codex")  # passes=2 == participant count -> paused
        self.assertEqual(r.returncode, 0)
        self.assertIn("chat paused", r.stdout)
        st2 = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("status: paused", st2.stdout)

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


class TestWaitPid(ChatTestCase):
    def test_wait_sets_wait_pid_while_blocked_and_clears_after(self):
        chat_id = self.setup_three_way_chat()  # speaker is claude; codex's wait blocks
        proc = subprocess.Popen(
            [PY, SCRIPT, "wait", "--chat", chat_id, "--name", "codex", "--timeout", "5"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.time() + 3
            pid = None
            while time.time() < deadline:
                rows = self.query(
                    "SELECT wait_pid FROM participants WHERE chat_id=? AND name='codex'", (chat_id,)
                )
                if rows and rows[0][0] is not None:
                    pid = rows[0][0]
                    break
                time.sleep(0.1)
            self.assertEqual(pid, proc.pid)
        finally:
            proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()

        rows = self.query(
            "SELECT wait_pid FROM participants WHERE chat_id=? AND name='codex'", (chat_id,)
        )
        self.assertIsNone(rows[0][0])

    def test_migration_adds_wait_pid_to_a_db_created_without_it(self):
        chat_id = self.create_chat(name="claude", topic="t")
        # simulate a pre-migration DB by rebuilding participants without wait_pid
        conn = sqlite3.connect(self.db_path())
        conn.execute("ALTER TABLE participants RENAME TO participants_old")
        conn.execute(
            "CREATE TABLE participants (chat_id TEXT, name TEXT, harness TEXT, position INTEGER, "
            "shown_seq INTEGER, read_seq INTEGER, UNIQUE(chat_id, name))"
        )
        conn.execute(
            "INSERT INTO participants (chat_id, name, harness, position, shown_seq, read_seq) "
            "SELECT chat_id, name, harness, position, shown_seq, read_seq FROM participants_old"
        )
        conn.execute("DROP TABLE participants_old")
        conn.commit()
        conn.close()

        r = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("status: lobby", r.stdout)
        cols = [row[1] for row in self.query("PRAGMA table_info(participants)")]
        self.assertIn("wait_pid", cols)


class TestStopHook(ChatTestCase):
    def test_stop_hook_active_always_passes(self):
        chat_id = self.setup_three_way_chat()
        self.bind("claude", "s1", chat_id, "claude")
        r = self.stop_hook("claude", "s1", stop_hook_active=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_unbound_session_passes(self):
        r = self.stop_hook("claude", "never-bound")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_lobby_chat_passes(self):
        chat_id = self.create_chat(name="claude", topic="t")
        self.bind("claude", "s1", chat_id, "claude")
        r = self.stop_hook("claude", "s1")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_at_limit_passes(self):
        chat_id = self.setup_three_way_chat(max_turns=2)
        self.post(chat_id, "claude", "a")
        self.post(chat_id, "codex", "b")  # at limit; speaker becomes claude (host)
        self.bind("claude", "s1", chat_id, "claude")
        r = self.stop_hook("claude", "s1")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_speaker_is_me_blocks_and_claude_mentions_wait(self):
        chat_id = self.setup_three_way_chat()  # speaker is claude
        self.bind("claude", "s1", chat_id, "claude")
        r = self.stop_hook("claude", "s1")
        data = json.loads(r.stdout)
        self.assertEqual(data["decision"], "block")
        self.assertIn("still your turn", data["reason"])
        self.assertIn("wait", data["reason"])

    def test_speaker_is_me_blocks_and_codex_does_not_mention_wait(self):
        chat_id = self.create_chat(name="codex", topic="t")
        self.join(chat_id, "claude")
        self.start(chat_id, "codex")  # speaker is codex
        self.bind_direct("codex", "s1", chat_id, "codex")
        r = self.stop_hook("codex", "s1")
        data = json.loads(r.stdout)
        self.assertEqual(data["decision"], "block")
        self.assertIn("still your turn", data["reason"])
        self.assertNotIn("background wait", data["reason"])

    def test_claude_not_speaker_and_no_live_wait_blocks(self):
        chat_id = self.setup_three_way_chat()  # speaker is claude
        self.bind("claude", "s1", chat_id, "codex")  # bound to a participant not currently speaking
        r = self.stop_hook("claude", "s1")
        data = json.loads(r.stdout)
        self.assertEqual(data["decision"], "block")
        self.assertIn("no wait is running", data["reason"])

    def test_claude_not_speaker_with_live_wait_passes(self):
        chat_id = self.setup_three_way_chat()  # speaker is claude
        self.bind("claude", "s1", chat_id, "codex")
        helper = subprocess.Popen([PY, "-c", "import time; time.sleep(5)"])
        try:
            self.set_wait_pid(chat_id, "codex", helper.pid)
            r = self.stop_hook("claude", "s1")
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")
        finally:
            helper.terminate()
            helper.wait(timeout=5)

    def test_claude_wait_registering_shortly_after_stop_passes(self):
        chat_id = self.setup_three_way_chat()  # speaker is claude
        self.bind("claude", "s1", chat_id, "codex")
        helper = subprocess.Popen([PY, "-c", "import time; time.sleep(10)"])
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(self.stop_hook, "claude", "s1")
                time.sleep(2)
                self.set_wait_pid(chat_id, "codex", helper.pid)
                r = fut.result(timeout=15)
            self.assertEqual(r.stdout, "")
        finally:
            helper.terminate()
            helper.wait(timeout=5)

    def test_codex_not_speaker_passes_regardless_of_wait_pid(self):
        chat_id = self.setup_three_way_chat()  # speaker is claude
        self.bind_direct("codex", "s1", chat_id, "opencode")  # not speaker, no wait_pid at all
        r = self.stop_hook("codex", "s1")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_paused_claude_without_live_wait_blocks(self):
        chat_id = self.setup_three_way_chat()
        self.pass_turn(chat_id, "claude")
        self.pass_turn(chat_id, "codex")
        self.pass_turn(chat_id, "opencode")  # paused, speaker (next-in-line) = claude
        self.bind("claude", "s1", chat_id, "claude")
        r = self.stop_hook("claude", "s1")
        data = json.loads(r.stdout)
        self.assertEqual(data["decision"], "block")
        self.assertIn("no wait is running", data["reason"])

    def test_paused_claude_with_live_wait_passes(self):
        chat_id = self.setup_three_way_chat()
        self.pass_turn(chat_id, "claude")
        self.pass_turn(chat_id, "codex")
        self.pass_turn(chat_id, "opencode")  # paused, speaker (next-in-line) = claude
        self.bind("claude", "s1", chat_id, "claude")
        helper = subprocess.Popen([PY, "-c", "import time; time.sleep(5)"])
        try:
            self.set_wait_pid(chat_id, "claude", helper.pid)
            r = self.stop_hook("claude", "s1")
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")
        finally:
            helper.terminate()
            helper.wait(timeout=5)


class TestRejoin(ChatTestCase):
    def test_rejoin_unknown_name_rejected(self):
        chat_id = self.setup_three_way_chat()
        r = self.join(chat_id, "someone-else", rejoin=True, check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_plain_join_of_taken_name_still_rejected(self):
        chat_id = self.setup_three_way_chat()
        r = self.join(chat_id, "codex", check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_rejoin_rebinds_session_and_delivers_since_read_seq(self):
        # bind_direct, not bind: only the session/participant bookkeeping is under
        # test here, not hook_bind's codex-waker spawn (covered by TestReopenCodexWaker).
        chat_id = self.setup_three_way_chat(max_turns=12)
        self.bind_direct("codex", "sess-old", chat_id, "codex")

        r = self.join(chat_id, "codex", rejoin=True, check=True)
        self.assertIn("MAC_BIND chat=%s name=codex" % chat_id, r.stdout)
        self.assertIn("roster: claude, codex, opencode", r.stdout)

        self.bind_direct("codex", "sess-new", chat_id, "codex")
        rows = self.query(
            "SELECT chat_id, name FROM sessions WHERE harness='codex' AND session_id='sess-new'"
        )
        self.assertEqual(rows, [(chat_id, "codex")])

        self.post(chat_id, "claude", "a")  # codex's turn now
        r = self.relay("codex", "sess-new", "note for codex")
        self.assertEqual(r.returncode, 0)
        self.assertIn("posted", json.loads(r.stdout)["reason"])

        w = self.wait(chat_id, "codex", timeout=2)
        self.assertEqual(w.returncode, 0)
        self.assertIn("note for codex", w.stdout)

    def test_rejoin_allowed_while_lobby(self):
        chat_id = self.create_chat(name="claude", topic="t")
        self.join(chat_id, "codex")
        r = self.join(chat_id, "codex", rejoin=True, check=False)
        self.assertEqual(r.returncode, 0)

    def test_rejoin_rejected_when_ended(self):
        chat_id = self.setup_three_way_chat()
        self.run_cmd(["end", "--chat", chat_id, "--name", "claude"], check=True)
        r = self.join(chat_id, "codex", rejoin=True, check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_rejoin_latest_ignores_status(self):
        chat_id = self.setup_three_way_chat()
        self.run_cmd(["end", "--chat", chat_id, "--name", "claude"], check=True)
        r = self.join(None, "codex", rejoin=True, latest=True, check=False)
        # --latest resolved to this (ended) chat rather than failing to find one;
        # rejoin is then rejected because the chat itself is not open to rejoin.
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("chat is not open to rejoin", r.stderr)


class TestReopen(ChatTestCase):
    def ended_chat(self, max_turns=2):
        chat_id = self.setup_three_way_chat(max_turns=max_turns)
        self.post(chat_id, "claude", "a")
        self.post(chat_id, "codex", "b")  # at limit; speaker=claude (host)
        self.post(chat_id, "claude", "closing summary")  # ends the chat
        return chat_id

    def test_reopen_on_active_chat_rejected(self):
        chat_id = self.setup_three_way_chat()
        r = self.reopen(chat_id, "claude", check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_reopen_by_non_participant_rejected(self):
        chat_id = self.ended_chat()
        r = self.reopen(chat_id, "nobody", check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_reopen_sets_speaker_and_max_turns_then_rotation_continues(self):
        chat_id = self.ended_chat(max_turns=2)  # turns_taken == 2
        r = self.reopen(chat_id, "codex", turns=3, check=True)
        self.assertIn("reopened", r.stdout)
        self.assertIn("MAC_BIND chat=%s name=codex" % chat_id, r.stdout)

        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("status: active", st.stdout)
        self.assertIn("speaker: codex", st.stdout)
        self.assertIn("turns: 2/5", st.stdout)
        tail = self.run_cmd(["tail", "--chat", chat_id], check=True)
        self.assertIn("chat reopened by codex (+3 turns)", tail.stdout)

        # another agent's wait blocks until the reopener posts
        r = self.wait(chat_id, "opencode", timeout=1)
        self.assertEqual(r.returncode, 4)

        r = self.post(chat_id, "codex", "continuing")
        self.assertEqual(r.returncode, 0)
        self.assertIn("next: opencode", r.stdout)  # rotation resumes after the reopener

    def test_reopen_default_turns_uses_config_max_turns(self):
        self.write_config(max_turns=7, turn_timeout_s=600, wait_timeout_s=300)
        chat_id = self.ended_chat(max_turns=2)
        self.reopen(chat_id, "claude", check=True)
        st = self.run_cmd(["status", "--chat", chat_id], check=True)
        self.assertIn("turns: 2/9", st.stdout)


class TestReopenCodexWaker(ChatTestCase):
    def setUp(self):
        super().setUp()
        self.codex_log = os.path.join(self.home, "fake-codex.log")
        self.codex_bin = os.path.join(self.home, "fake-codex.sh")
        with open(self.codex_bin, "w") as f:
            f.write('#!/bin/sh\necho "$@" >> "%s"\nexit 0\n' % self.codex_log)
        os.chmod(self.codex_bin, 0o755)
        self.env["MAC_CODEX_BIN"] = self.codex_bin

    def wait_until(self, predicate, timeout, message):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail(message)

    def test_codex_bind_after_reopen_spawns_a_waker(self):
        chat_id = self.create_chat(name="codex", topic="t", max_turns=2)
        self.join(chat_id, "claude")
        self.start(chat_id, "codex")
        self.post(chat_id, "codex", "a")
        self.post(chat_id, "claude", "b")  # at limit; speaker=codex (host)
        self.post(chat_id, "codex", "closing summary")  # ends the chat

        pidfile = os.path.join(self.home, "data", "wakers", "%s-codex.pid" % chat_id)
        self.assertFalse(os.path.exists(pidfile))  # never bound while active, so never spawned

        self.reopen(chat_id, "codex", check=True)
        self.bind("codex", "codex-sess-reopen", chat_id, "codex")
        self.wait_until(lambda: os.path.exists(pidfile), 5, "waker did not spawn after reopen")

        self.run_cmd(["end", "--chat", chat_id, "--name", "codex"], check=True)
        self.wait_until(lambda: not os.path.exists(pidfile), 8, "waker did not exit after chat ended")


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
                ],
                "Stop": [
                    {"hooks": [{"type": "command", "command": "/bin/echo existing-stop"}]}
                ],
            },
        }
        codex_hooks = {
            "description": "codex hooks",
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "/bin/echo session-start"}]}],
                "PostToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "/bin/echo audor-hook"}]}
                ],
                "Stop": [
                    {"hooks": [{"type": "command", "command": "/bin/echo existing-codex-stop"}]}
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
        claude_stop = claude["hooks"]["Stop"]
        self.assertEqual(self.count_mac_groups(claude_stop), 1)
        self.assertTrue(any("existing-stop" in h["command"] for g in claude_stop for h in g["hooks"]))

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
        codex_stop = codex["hooks"]["Stop"]
        self.assertEqual(self.count_mac_groups(codex_stop), 1)
        self.assertTrue(
            any("existing-codex-stop" in h["command"] for g in codex_stop for h in g["hooks"])
        )

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
