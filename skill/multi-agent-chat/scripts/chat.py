#!/usr/bin/env python3
"""multi-agent-chat store + CLI. Stdlib only, targets /usr/bin/python3 (3.9)."""

import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time

DEFAULT_CONFIG = {"max_turns": 12, "turn_timeout_s": 600, "wait_timeout_s": 300}
BRIEF_MAX_BYTES = 16384
MENTION_RE = re.compile(r"(?<![\w@])@([a-z0-9](?:[a-z0-9_-]*[a-z0-9])?)", re.IGNORECASE)
BIND_RE = re.compile(r"MAC_BIND chat=([0-9]{8}-[0-9a-f]{4}) name=([a-z0-9][a-z0-9_-]*)")
# <harness>-<model>, e.g. claude-opus; hyphens allowed inside, never at the ends.
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?$")
TURN_HEADER_RE = re.compile(r"your turn \((\d+)/\d+\)")
CODEX_WAKER_TIMEOUT_S = 300
CODEX_QUEUE_RETRY_S = 5


def get_home():
    return os.environ.get("MAC_HOME") or os.path.expanduser("~/.multi-agent-chat")


def get_config(home):
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(home, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (IOError, ValueError):
            pass
    return cfg


def get_db(home):
    data_dir = os.path.join(home, "data")
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(data_dir, "chat.db"), isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    init_schema(conn)
    return conn


def init_schema(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY, topic TEXT, brief TEXT, host TEXT, status TEXT,
        speaker TEXT, turn_started_at REAL, next_speaker TEXT,
        turns_taken INTEGER, max_turns INTEGER, turn_timeout_s INTEGER, created_at REAL
    )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS participants (
        chat_id TEXT, name TEXT, harness TEXT, position INTEGER,
        shown_seq INTEGER, read_seq INTEGER, UNIQUE(chat_id, name)
    )"""
    )
    try:
        # wait_pid: liveness marker for cmd_wait (see hook_stop's safety net). Added via
        # ALTER rather than the CREATE TABLE above because existing DBs predate this column.
        conn.execute("ALTER TABLE participants ADD COLUMN wait_pid INTEGER")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
        chat_id TEXT, seq INTEGER, ts REAL, sender TEXT, kind TEXT, via TEXT, text TEXT,
        UNIQUE(chat_id, seq)
    )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
        harness TEXT, session_id TEXT, chat_id TEXT, name TEXT,
        PRIMARY KEY (harness, session_id)
    )"""
    )


def fail(msg, code=1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def validate_name(name):
    """Mention parsing (MENTION_RE) assumes names are plain \\w tokens."""
    if not NAME_RE.match(name):
        fail("invalid name %r: must match ^[a-z0-9_]{1,32}$" % name)


def with_txn(conn, fn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn()
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return result


def gen_chat_id(conn):
    for _ in range(20):
        cid = time.strftime("%Y%m%d") + "-" + "%04x" % random.randint(0, 0xFFFF)
        if conn.execute("SELECT 1 FROM chats WHERE id=?", (cid,)).fetchone() is None:
            return cid
    fail("could not generate a unique chat id")


def get_chat(conn, chat_id):
    return conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()


def get_participant(conn, chat_id, name):
    return conn.execute(
        "SELECT * FROM participants WHERE chat_id=? AND name=?", (chat_id, name)
    ).fetchone()


def roster(conn, chat_id):
    return conn.execute(
        "SELECT name FROM participants WHERE chat_id=? ORDER BY position", (chat_id,)
    ).fetchall()


def resolve_chat_id(conn, chat_id):
    if chat_id:
        return chat_id
    row = conn.execute("SELECT id FROM chats ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    if row is None:
        fail("no chat found (pass --chat)")
    return row["id"]


def add_message(conn, chat_id, sender, kind, via, text):
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE chat_id=?", (chat_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO messages (chat_id, seq, ts, sender, kind, via, text) VALUES (?,?,?,?,?,?,?)",
        (chat_id, seq, time.time(), sender, kind, via, text),
    )
    return seq


def add_system_message(conn, chat_id, text):
    return add_message(conn, chat_id, "system", "system", None, text)


def pick_next_speaker(conn, chat_id, current_speaker, next_speaker_override):
    if next_speaker_override:
        if get_participant(conn, chat_id, next_speaker_override) is not None:
            return next_speaker_override
    names = [r["name"] for r in roster(conn, chat_id)]
    if not names:
        return None
    if current_speaker not in names:
        return names[0]
    idx = names.index(current_speaker)
    return names[(idx + 1) % len(names)]


def advance_turn(conn, chat_id, current_speaker, next_speaker_override):
    new_speaker = pick_next_speaker(conn, chat_id, current_speaker, next_speaker_override)
    conn.execute(
        "UPDATE chats SET speaker=?, next_speaker=NULL, turn_started_at=? WHERE id=?",
        (new_speaker, time.time(), chat_id),
    )
    return new_speaker


def bump_turns_taken(conn, chat_id, chat):
    """Increment turns_taken. Reaching max_turns puts the chat "at limit": it stays
    active, but the turn passes to the host and only the host may act (see `extend`
    and `cmd_post`'s at-limit branch). Returns True if the chat is now at limit."""
    turns_taken = chat["turns_taken"] + 1
    conn.execute("UPDATE chats SET turns_taken=? WHERE id=?", (turns_taken, chat_id))
    if turns_taken >= chat["max_turns"]:
        conn.execute(
            "UPDATE chats SET speaker=?, next_speaker=NULL, turn_started_at=? WHERE id=?",
            (chat["host"], time.time(), chat_id),
        )
        add_system_message(
            conn, chat_id,
            "turn limit reached (%d turns) · host decides: close or ask the human to extend"
            % chat["max_turns"],
        )
        return True
    return False


def check_and_apply_timeout(conn, chat):
    """Lazy per-command check: skip a stalled turn once, if overdue.
    At limit, the chat waits for the host indefinitely; the timeout never fires."""
    if chat is None or chat["status"] != "active" or chat["turn_started_at"] is None:
        return chat
    if chat["turns_taken"] >= chat["max_turns"]:
        return chat
    if time.time() - chat["turn_started_at"] <= chat["turn_timeout_s"]:
        return chat
    speaker = chat["speaker"]
    add_system_message(
        conn, chat["id"], "skipped %s (no reply in %ds)" % (speaker, chat["turn_timeout_s"])
    )
    at_limit = bump_turns_taken(conn, chat["id"], chat)
    if not at_limit:
        advance_turn(conn, chat["id"], speaker, chat["next_speaker"])
    return get_chat(conn, chat["id"])


def add_human(conn, chat, via, text):
    """Insert a human message; @mentions set next_speaker (last one wins)."""
    seq = add_message(conn, chat["id"], "human", "human", via, text)
    names = set(r["name"] for r in roster(conn, chat["id"]))
    mentioned = [m.lower() for m in MENTION_RE.findall(text) if m.lower() in names]
    if mentioned:
        conn.execute("UPDATE chats SET next_speaker=? WHERE id=?", (mentioned[-1], chat["id"]))
    return seq


def format_message(row):
    if row["kind"] == "agent":
        return "[%d] %s (agent): %s" % (row["seq"], row["sender"], row["text"])
    if row["kind"] == "human":
        return "[%d] human via %s: %s" % (row["seq"], row["via"], row["text"])
    return "[%d] system: %s" % (row["seq"], row["text"])


def read_text_arg(text_arg, file_arg):
    if file_arg:
        if file_arg == "-":
            return sys.stdin.read()
        with open(file_arg, "r", encoding="utf-8") as f:
            return f.read()
    if text_arg is None:
        fail("either TEXT or --file is required")
    return text_arg


# ---- commands ----


def cmd_create(args, home, conn):
    validate_name(args.name)
    cfg = get_config(home)
    max_turns = args.max_turns if args.max_turns is not None else cfg["max_turns"]
    chat_id = gen_chat_id(conn)

    def txn():
        conn.execute(
            "INSERT INTO chats (id, topic, brief, host, status, speaker, turn_started_at, "
            "next_speaker, turns_taken, max_turns, turn_timeout_s, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                chat_id, args.topic, None, args.name, "lobby", None, None, None,
                0, max_turns, cfg["turn_timeout_s"], time.time(),
            ),
        )
        conn.execute(
            "INSERT INTO participants (chat_id, name, harness, position, shown_seq, read_seq) "
            "VALUES (?,?,?,0,0,0)",
            (chat_id, args.name, args.harness or ""),
        )

    with_txn(conn, txn)
    print(chat_id)
    print("MAC_BIND chat=%s name=%s" % (chat_id, args.name))
    return 0


def cmd_brief(args, home, conn):
    chat_id = resolve_chat_id(conn, args.chat)
    data = sys.stdin.buffer.read() if args.file == "-" else open(args.file, "rb").read()
    if len(data) > BRIEF_MAX_BYTES:
        fail("brief exceeds %d bytes" % BRIEF_MAX_BYTES)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        fail("brief must be valid UTF-8")

    def txn():
        chat = get_chat(conn, chat_id)
        if chat is None:
            fail("chat %s not found" % chat_id)
        chat = check_and_apply_timeout(conn, chat)
        if chat["host"] != args.name:
            fail("only the host can set the brief")
        if chat["status"] != "lobby":
            fail("brief can only be set before the chat starts")
        conn.execute("UPDATE chats SET brief=? WHERE id=?", (text, chat_id))

    with_txn(conn, txn)
    print("brief set")
    return 0


def cmd_join(args, home, conn):
    validate_name(args.name)
    if args.latest:
        chat_id = resolve_chat_id(conn, None)
    elif args.chat_id:
        chat_id = args.chat_id
    else:
        fail("pass a chat id or --latest")

    def txn():
        chat = get_chat(conn, chat_id)
        if chat is None:
            fail("chat %s not found" % chat_id)
        chat = check_and_apply_timeout(conn, chat)
        existing = get_participant(conn, chat_id, args.name)
        if args.rejoin:
            # Rebinding an existing participant's name to a new session (e.g. a
            # restarted harness): no roster change, just a fresh MAC_BIND below.
            if existing is None:
                fail("%s is not a participant in chat %s" % (args.name, chat_id))
            if chat["status"] not in ("lobby", "active"):
                fail("chat is not open to rejoin")
            if args.harness:
                conn.execute(
                    "UPDATE participants SET harness=? WHERE chat_id=? AND name=?",
                    (args.harness, chat_id, args.name),
                )
        else:
            if chat["status"] != "lobby":
                fail("chat is not in lobby")
            if existing is not None:
                fail("name %s is already taken in this chat" % args.name)
            pos = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM participants WHERE chat_id=?", (chat_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO participants (chat_id, name, harness, position, shown_seq, read_seq) "
                "VALUES (?,?,?,?,0,0)",
                (chat_id, args.name, args.harness or "", pos),
            )
        return chat

    chat = with_txn(conn, txn)
    print("topic: %s" % chat["topic"])
    print("brief:")
    print(chat["brief"] if chat["brief"] else "(none)")
    print("roster: %s" % ", ".join(r["name"] for r in roster(conn, chat_id)))
    print("MAC_BIND chat=%s name=%s" % (chat_id, args.name))
    return 0


def cmd_start(args, home, conn):
    chat_id = resolve_chat_id(conn, args.chat)

    def txn():
        chat = get_chat(conn, chat_id)
        if chat is None:
            fail("chat %s not found" % chat_id)
        chat = check_and_apply_timeout(conn, chat)
        if chat["host"] != args.name:
            fail("only the host can start the chat")
        if chat["status"] != "lobby":
            fail("chat already started")
        names = [r["name"] for r in roster(conn, chat_id)]
        conn.execute(
            "UPDATE chats SET status='active', speaker=?, turn_started_at=?, next_speaker=NULL "
            "WHERE id=?",
            (chat["host"], time.time(), chat_id),
        )
        add_system_message(conn, chat_id, "chat started · order: %s" % " → ".join(names))

    with_txn(conn, txn)
    print("chat %s started" % chat_id)
    return 0


def format_turn_text(chat_id, name, chat, messages, next_after):
    at_limit = chat["turns_taken"] >= chat["max_turns"]
    if at_limit:
        header = "[multi-agent-chat] chat %s · your turn (%d/%d) · turn limit reached — you are the host" % (
            chat_id, chat["turns_taken"] + 1, chat["max_turns"],
        )
    else:
        header = "[multi-agent-chat] chat %s · your turn (%d/%d) · next: %s" % (
            chat_id, chat["turns_taken"] + 1, chat["max_turns"], next_after,
        )
    lines = [header]
    lines.extend(format_message(row) for row in messages)
    script_path = os.path.abspath(sys.argv[0])
    if at_limit:
        lines.append(
            "If the discussion is finished: post a closing summary (this ends the chat):\n"
            "  python3 %s post --chat %s --name %s --file - <<'EOF' ... EOF\n"
            "Otherwise ask the human in your session whether to extend, telling them to answer "
            'privately with "// extend N" or "// end the chat". Then run:\n'
            "  python3 %s extend --chat %s --name %s --turns N   (and take your turn)\n"
            "  or python3 %s end --chat %s --name %s"
            % (script_path, chat_id, name, script_path, chat_id, name, script_path, chat_id, name)
        )
    else:
        lines.append(
            'Reply: python3 %s post --chat %s --name %s "<text>"   (or --file PATH / --file -)'
            % (script_path, chat_id, name)
        )
    return "\n".join(lines)


def wait_for_turn(conn, chat_id, name, timeout, skip_turn):
    """Block until it's `name`'s turn, the chat ends, or `timeout` elapses.

    `skip_turn`, when set, is a turn number (turns_taken+1) already delivered:
    that turn is not returned again, so a caller re-polling after delivering it
    doesn't re-deliver the same turn while waiting for the post that ends it.
    Returns (exit_code, text): 0 (turn, text is the turn block), 3 (ended) or
    4 (timed out; text is a one-line status message).
    """
    deadline = time.time() + timeout

    def check():
        chat = get_chat(conn, chat_id)
        if chat is None:
            fail("chat %s not found" % chat_id)
        chat = check_and_apply_timeout(conn, chat)
        if chat["status"] == "ended":
            return ("ended", None)
        if chat["status"] == "active" and chat["speaker"] == name:
            turn_number = chat["turns_taken"] + 1
            if skip_turn is not None and turn_number == skip_turn:
                return (None, None)
            participant = get_participant(conn, chat_id, name)
            messages = conn.execute(
                "SELECT * FROM messages WHERE chat_id=? AND seq>? AND sender!=? ORDER BY seq",
                (chat_id, participant["read_seq"], name),
            ).fetchall()
            max_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), ?) FROM messages WHERE chat_id=?",
                (participant["read_seq"], chat_id),
            ).fetchone()[0]
            conn.execute(
                "UPDATE participants SET shown_seq=? WHERE chat_id=? AND name=?",
                (max_seq, chat_id, name),
            )
            next_after = pick_next_speaker(conn, chat_id, name, chat["next_speaker"])
            return ("turn", (chat, messages, next_after))
        return (None, None)

    while True:
        kind, payload = with_txn(conn, check)
        if kind == "ended":
            return 3, "[multi-agent-chat] chat %s ended" % chat_id
        if kind == "turn":
            chat, messages, next_after = payload
            return 0, format_turn_text(chat_id, name, chat, messages, next_after)
        if time.time() >= deadline:
            return 4, "[multi-agent-chat] chat %s · wait timed out" % chat_id
        time.sleep(1)


def set_wait_pid(conn, chat_id, name, pid):
    conn.execute(
        "UPDATE participants SET wait_pid=? WHERE chat_id=? AND name=?", (pid, chat_id, name)
    )


def cmd_wait(args, home, conn):
    chat_id = resolve_chat_id(conn, args.chat)
    cfg = get_config(home)
    timeout = args.timeout if args.timeout is not None else cfg["wait_timeout_s"]
    # Liveness marker for hook_stop's safety net: only cmd_wait sets this (not the
    # codex waker, which is a distinct long-lived process the stop hook doesn't track).
    with_txn(conn, lambda: set_wait_pid(conn, chat_id, args.name, os.getpid()))
    try:
        code, text = wait_for_turn(conn, chat_id, args.name, timeout, args.skip_turn)
    finally:
        with_txn(conn, lambda: set_wait_pid(conn, chat_id, args.name, None))
    print(text)
    return code


def cmd_post(args, home, conn):
    chat_id = resolve_chat_id(conn, args.chat)
    text = read_text_arg(args.text, args.file)

    def txn():
        chat = get_chat(conn, chat_id)
        if chat is None:
            fail("chat %s not found" % chat_id)
        chat = check_and_apply_timeout(conn, chat)
        if chat["status"] != "active":
            fail("chat is not active", code=2)
        if chat["speaker"] != args.name:
            fail("not your turn (speaker: %s)" % chat["speaker"], code=2)
        at_limit = chat["turns_taken"] >= chat["max_turns"]
        participant = get_participant(conn, chat_id, args.name)
        seq = add_message(conn, chat_id, args.name, "agent", None, text)
        conn.execute(
            "UPDATE participants SET read_seq=? WHERE chat_id=? AND name=?",
            (participant["shown_seq"], chat_id, args.name),
        )
        if at_limit:
            # host's post while at limit is the close-or-continue decision: it always ends the chat.
            conn.execute("UPDATE chats SET status='ended' WHERE id=?", (chat_id,))
            add_system_message(conn, chat_id, "chat closed by %s" % args.name)
            return seq, None, True
        new_at_limit = bump_turns_taken(conn, chat_id, chat)
        if new_at_limit:
            return seq, chat["host"], False
        new_speaker = advance_turn(conn, chat_id, args.name, chat["next_speaker"])
        return seq, new_speaker, False

    seq, new_speaker, ended = with_txn(conn, txn)
    if ended:
        print("posted #%d · chat ended" % seq)
    else:
        print("posted #%d · next: %s" % (seq, new_speaker))
    return 0


def cmd_end(args, home, conn):
    chat_id = resolve_chat_id(conn, args.chat)

    def txn():
        chat = get_chat(conn, chat_id)
        if chat is None:
            fail("chat %s not found" % chat_id)
        if chat["status"] == "ended":
            return
        conn.execute("UPDATE chats SET status='ended' WHERE id=?", (chat_id,))
        add_system_message(conn, chat_id, "chat ended by %s" % (args.name or "human"))

    with_txn(conn, txn)
    print("chat %s ended" % chat_id)
    return 0


def cmd_reopen(args, home, conn):
    chat_id = resolve_chat_id(conn, args.chat)
    cfg = get_config(home)
    turns_add = args.turns if args.turns is not None else cfg["max_turns"]

    def txn():
        chat = get_chat(conn, chat_id)
        if chat is None:
            fail("chat %s not found" % chat_id)
        if get_participant(conn, chat_id, args.name) is None:
            fail("%s is not a participant in chat %s" % (args.name, chat_id))
        if chat["status"] != "ended":
            fail("chat is not ended")
        new_max = chat["turns_taken"] + turns_add
        conn.execute(
            "UPDATE chats SET status='active', max_turns=?, speaker=?, next_speaker=NULL, "
            "turn_started_at=? WHERE id=?",
            (new_max, args.name, time.time(), chat_id),
        )
        add_system_message(
            conn, chat_id, "chat reopened by %s (+%d turns)" % (args.name, turns_add)
        )

    with_txn(conn, txn)
    print("chat %s reopened" % chat_id)
    print("MAC_BIND chat=%s name=%s" % (chat_id, args.name))
    return 0


def cmd_extend(args, home, conn):
    chat_id = resolve_chat_id(conn, args.chat)
    if args.turns < 1:
        fail("--turns must be >= 1")

    def txn():
        chat = get_chat(conn, chat_id)
        if chat is None:
            fail("chat %s not found" % chat_id)
        chat = check_and_apply_timeout(conn, chat)
        if get_participant(conn, chat_id, args.name) is None:
            fail("%s is not a participant in chat %s" % (args.name, chat_id))
        if chat["status"] != "active":
            fail("chat is not active")
        at_limit = chat["turns_taken"] >= chat["max_turns"]
        new_max = chat["max_turns"] + args.turns
        conn.execute("UPDATE chats SET max_turns=? WHERE id=?", (new_max, chat_id))
        if at_limit:
            # the host's turn only just started for real (it was parked waiting on a
            # human decision); give it a fresh timeout clock instead of the stale one.
            conn.execute("UPDATE chats SET turn_started_at=? WHERE id=?", (time.time(), chat_id))
        add_system_message(
            conn, chat_id,
            "%s extended the chat by %d turns (now %d)" % (args.name, args.turns, new_max),
        )
        return new_max

    new_max = with_txn(conn, txn)
    print("chat %s extended to %d turns" % (chat_id, new_max))
    return 0


def cmd_status(args, home, conn):
    chat_id = resolve_chat_id(conn, args.chat)

    def txn():
        chat = get_chat(conn, chat_id)
        if chat is None:
            fail("chat %s not found" % chat_id)
        return check_and_apply_timeout(conn, chat)

    chat = with_txn(conn, txn)
    print("id: %s" % chat["id"])
    print("topic: %s" % chat["topic"])
    print("status: %s" % chat["status"])
    print("roster: %s" % ", ".join(r["name"] for r in roster(conn, chat_id)))
    print("speaker: %s" % chat["speaker"])
    at_limit = chat["status"] == "active" and chat["turns_taken"] >= chat["max_turns"]
    if at_limit:
        print("turns: %d/%d (at limit — waiting for host)" % (chat["turns_taken"], chat["max_turns"]))
    else:
        print("turns: %d/%d" % (chat["turns_taken"], chat["max_turns"]))
    return 0


# Distinct ANSI colors per sender; applied only when stdout is a terminal.
TAIL_COLORS = ["36", "33", "35", "32", "34"]


def format_tail_message(row, color_for):
    if row["kind"] == "human":
        who = "human (via %s)" % row["via"]
    else:
        who = row["sender"]
    header = "── [%d] %s · %s ──" % (row["seq"], who, time.strftime("%H:%M:%S", time.localtime(row["ts"])))
    code = color_for(row)
    if code:
        header = "\033[1;%sm%s\033[0m" % (code, header)
    return header + "\n" + row["text"].rstrip() + "\n"


def cmd_tail(args, home, conn):
    chat_id = resolve_chat_id(conn, args.chat)
    last_seq = 0
    colors = {}

    def color_for(row):
        if not sys.stdout.isatty():
            return None
        if row["kind"] == "system":
            return "90"
        if row["kind"] == "human":
            return "31"
        return colors.setdefault(row["sender"], TAIL_COLORS[len(colors) % len(TAIL_COLORS)])

    def note(text):
        # grey/90, matching format_tail_message's system-message color
        print("\033[1;90m%s\033[0m" % text if sys.stdout.isatty() else text)

    def fetch_chat():
        def txn():
            chat = get_chat(conn, chat_id)
            if chat is None:
                fail("chat %s not found" % chat_id)
            return check_and_apply_timeout(conn, chat)

        return with_txn(conn, txn)

    def current_roster():
        return ", ".join(r["name"] for r in roster(conn, chat_id))

    # A lobby chat has no messages yet, so tail would otherwise print nothing at all.
    chat = fetch_chat()
    prev_status = chat["status"]
    prev_roster = current_roster()
    note("chat %s · %s · %s · %s" % (chat_id, prev_status, prev_roster, chat["topic"][:80]))

    first = True
    while True:
        if not first:
            chat = fetch_chat()
            if chat["status"] != prev_status:
                note("· status: %s" % chat["status"])
                prev_status = chat["status"]
            if chat["status"] == "lobby":
                cur_roster = current_roster()
                if cur_roster != prev_roster:
                    note("· roster: %s" % cur_roster)
                    prev_roster = cur_roster
        first = False

        rows = conn.execute(
            "SELECT * FROM messages WHERE chat_id=? AND seq>? ORDER BY seq", (chat_id, last_seq)
        ).fetchall()
        for row in rows:
            print(format_tail_message(row, color_for))
            last_seq = row["seq"]
        sys.stdout.flush()
        if not args.follow or chat["status"] == "ended":
            return 0
        time.sleep(1)


# ---- codex waker ----


def wakers_dir(home):
    d = os.path.join(home, "data", "wakers")
    os.makedirs(d, exist_ok=True)
    return d


def waker_pidfile(home, chat_id, name):
    return os.path.join(wakers_dir(home), "%s-%s.pid" % (chat_id, name))


def waker_logfile(home, chat_id, name):
    return os.path.join(wakers_dir(home), "%s-%s.log" % (chat_id, name))


def waker_is_alive(pidfile):
    if not os.path.exists(pidfile):
        return False
    try:
        with open(pidfile, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (IOError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def resolve_codex_bin():
    return os.environ.get("MAC_CODEX_BIN") or shutil.which("codex") or "/opt/homebrew/bin/codex"


def spawn_codex_waker(home, chat_id, name, thread):
    pidfile = waker_pidfile(home, chat_id, name)
    if waker_is_alive(pidfile):
        return
    script_path = os.path.abspath(sys.argv[0])
    log_path = waker_logfile(home, chat_id, name)
    with open(log_path, "a", encoding="utf-8") as logf:
        subprocess.Popen(
            [sys.executable, script_path, "waker", "codex", "--chat", chat_id, "--name", name,
             "--thread", thread],
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )


def cmd_waker(args, home, conn):
    if args.harness != "codex":
        fail("unknown waker harness: %s" % args.harness)
    chat_id, name = args.chat, args.name
    pidfile = waker_pidfile(home, chat_id, name)
    if waker_is_alive(pidfile):
        fail("a waker for %s/%s is already running" % (chat_id, name))
    with open(pidfile, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    codex_bin = resolve_codex_bin()
    log_path = waker_logfile(home, chat_id, name)
    last_delivered = None
    try:
        while True:
            code, text = wait_for_turn(conn, chat_id, name, CODEX_WAKER_TIMEOUT_S, last_delivered)
            if code == 3:
                return 0
            if code == 4:
                continue
            m = TURN_HEADER_RE.search(text)
            turn_number = int(m.group(1)) if m else None
            try:
                subprocess.run([codex_bin, "queue", "--thread", args.thread, "--message", text],
                                check=True)
                last_delivered = turn_number
            except (OSError, subprocess.CalledProcessError) as e:
                with open(log_path, "a", encoding="utf-8") as logf:
                    logf.write("%s codex queue failed: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), e))
                time.sleep(CODEX_QUEUE_RETRY_S)
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass


# ---- hooks ----


def extract_claude(data):
    return data.get("session_id"), data.get("prompt") or ""


def extract_codex(data):
    return data.get("session_id"), data.get("prompt") or ""


def extract_opencode(data):
    return data.get("session_id"), data.get("prompt") or ""


# Kept as one dict of small per-harness functions: hook field names may be
# remapped later, so a change is isolated to one function.
EXTRACTORS = {"claude": extract_claude, "codex": extract_codex, "opencode": extract_opencode}


def emit_pass(harness):
    if harness == "opencode":
        print(json.dumps({"action": "pass", "message": ""}))


def emit_block(harness, message):
    if harness == "opencode":
        print(json.dumps({"action": "block", "message": message}))
    else:
        print(json.dumps({"decision": "block", "reason": message}))


def log_hook_error(home, event, exc):
    path = os.path.join(home, "data", "hook-errors.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), event, exc))
    except OSError:
        pass


def cmd_hook(args, home, conn_unused):
    data = json.load(sys.stdin)
    if args.event == "relay":
        hook_relay(args.harness, data, home)
    elif args.event == "bind":
        hook_bind(args.harness, data, home)
    elif args.event == "stop":
        # Never trap the agent on our own bug: any failure here passes silently.
        try:
            hook_stop(args.harness, data, home)
        except Exception as e:
            log_hook_error(home, "stop", e)
    else:
        fail("unknown hook event: %s" % args.event)
    return 0


def strip_wrapping_quotes(text):
    """opencode's `run` wraps the injected prompt in double quotes."""
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def hook_relay(harness, data, home):
    extractor = EXTRACTORS.get(harness, extract_claude)
    session_id, prompt = extractor(data)
    prompt = strip_wrapping_quotes(prompt)

    db_path = os.path.join(home, "data", "chat.db")
    if not os.path.exists(db_path):
        return emit_pass(harness)

    conn = get_db(home)
    session = conn.execute(
        "SELECT chat_id, name FROM sessions WHERE harness=? AND session_id=?", (harness, session_id)
    ).fetchone()
    if session is None:
        return emit_pass(harness)

    chat_id, name = session["chat_id"], session["name"]
    try:
        chat = get_chat(conn, chat_id)  # None (chat row missing) falls through to the except below
        if chat["status"] != "active":
            # lobby: the human is setting up the chat with the host, e.g. "start the chat" —
            # that must reach the host normally, not be posted or blocked.
            return emit_pass(harness)
        stripped = prompt.lstrip()
        # Claude Code delivers background-task completions (the wake for a
        # background `wait`) through UserPromptSubmit as <task-notification>.
        if stripped.startswith(("[multi-agent-chat]", "//", "<task-notification>")):
            return emit_pass(harness)

        def txn():
            c = check_and_apply_timeout(conn, get_chat(conn, chat_id))
            return add_human(conn, c, name, prompt)

        seq = with_txn(conn, txn)
        return emit_block(harness, "posted to chat %s as #%d" % (chat_id, seq))
    except Exception as e:  # session was found bound; any failure from here on blocks
        return emit_block(harness, "multi-agent-chat relay failed: %s — message NOT posted" % e)


def hook_bind(harness, data, home):
    session_id = data.get("session_id")
    tool_response = data.get("tool_response")
    text = tool_response if isinstance(tool_response, str) else json.dumps(tool_response)
    m = BIND_RE.search(text)
    if m is not None:
        chat_id, name = m.group(1), m.group(2)
        conn = get_db(home)

        def txn():
            conn.execute(
                "INSERT INTO sessions (harness, session_id, chat_id, name) VALUES (?,?,?,?) "
                "ON CONFLICT(harness, session_id) DO UPDATE SET chat_id=excluded.chat_id, "
                "name=excluded.name",
                (harness, session_id, chat_id, name),
            )

        with_txn(conn, txn)
        if harness == "codex":
            spawn_codex_waker(home, chat_id, name, session_id)
    if harness == "opencode":
        print(json.dumps({"action": "pass"}))


def pid_is_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_is_running(conn, chat_id, name, grace_s=5.0):
    # The agent typically launches its background wait and ends its response at
    # once, so Stop can fire before that wait has recorded its pid.
    deadline = time.time() + grace_s
    while True:
        participant = get_participant(conn, chat_id, name)
        if participant is not None and pid_is_alive(participant["wait_pid"]):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def hook_stop(harness, data, home):
    """Stop-hook safety net: catches an agent that ended its turn without posting
    and without re-arming its wake, which would otherwise leave it unreachable.
    stop_hook_active means the harness is already continuing because of a
    previous block from this same hook -- always pass then, or it loops forever.
    """
    if data.get("stop_hook_active"):
        return emit_pass(harness)

    db_path = os.path.join(home, "data", "chat.db")
    if not os.path.exists(db_path):
        return emit_pass(harness)

    conn = get_db(home)
    session = conn.execute(
        "SELECT chat_id, name FROM sessions WHERE harness=? AND session_id=?",
        (harness, data.get("session_id")),
    ).fetchone()
    if session is None:
        return emit_pass(harness)

    chat_id, name = session["chat_id"], session["name"]
    chat = get_chat(conn, chat_id)
    if chat is None or chat["status"] != "active":
        return emit_pass(harness)
    if chat["turns_taken"] >= chat["max_turns"]:
        return emit_pass(harness)  # at limit: the host is waiting for the human

    script_path = os.path.abspath(sys.argv[0])
    if chat["speaker"] == name:
        reason = (
            "[multi-agent-chat] It is still your turn in chat %s. Post your reply now "
            "(if you have nothing to add, post a one-line pass): "
            "python3 %s post --chat %s --name %s --file - <<'EOF' ... EOF"
            % (chat_id, script_path, chat_id, name)
        )
        if harness == "claude":
            reason += (
                " Then start the background wait again: "
                "python3 %s wait --chat %s --name %s --timeout 3600 (run_in_background)."
                % (script_path, chat_id, name)
            )
        return emit_block(harness, reason)

    if harness == "claude" and not wait_is_running(conn, chat_id, name):
        reason = (
            "[multi-agent-chat] You are in chat %s and no wait is running, so you will "
            "never be woken for your turn. Start it now as a background command: "
            "python3 %s wait --chat %s --name %s --timeout 3600 (run_in_background: true), "
            "then end your response." % (chat_id, script_path, chat_id, name)
        )
        return emit_block(harness, reason)

    return emit_pass(harness)


# ---- argument parsing ----


def build_parser():
    p = argparse.ArgumentParser(prog="chat.py")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("create")
    c.add_argument("--name", required=True)
    c.add_argument("--topic", required=True)
    c.add_argument("--max-turns", type=int, dest="max_turns", default=None)
    c.add_argument("--harness", default=None)
    c.set_defaults(func=cmd_create)

    b = sub.add_parser("brief")
    b.add_argument("--chat", default=None)
    b.add_argument("--name", required=True)
    b.add_argument("--file", required=True)
    b.set_defaults(func=cmd_brief)

    j = sub.add_parser("join")
    j.add_argument("chat_id", nargs="?", default=None)
    j.add_argument("--latest", action="store_true")
    j.add_argument("--name", required=True)
    j.add_argument("--harness", default=None)
    j.add_argument("--rejoin", action="store_true")
    j.set_defaults(func=cmd_join)

    s = sub.add_parser("start")
    s.add_argument("--chat", default=None)
    s.add_argument("--name", required=True)
    s.set_defaults(func=cmd_start)

    w = sub.add_parser("wait")
    w.add_argument("--chat", default=None)
    w.add_argument("--name", required=True)
    w.add_argument("--timeout", type=float, default=None)
    w.add_argument("--skip-turn", type=int, dest="skip_turn", default=None)
    w.set_defaults(func=cmd_wait)

    po = sub.add_parser("post")
    po.add_argument("--chat", default=None)
    po.add_argument("--name", required=True)
    po.add_argument("text", nargs="?", default=None)
    po.add_argument("--file", default=None)
    po.set_defaults(func=cmd_post)

    e = sub.add_parser("end")
    e.add_argument("--chat", default=None)
    e.add_argument("--name", default=None)
    e.set_defaults(func=cmd_end)

    ro = sub.add_parser("reopen")
    ro.add_argument("--chat", default=None)
    ro.add_argument("--name", required=True)
    ro.add_argument("--turns", type=int, default=None)
    ro.set_defaults(func=cmd_reopen)

    ex = sub.add_parser("extend")
    ex.add_argument("--chat", default=None)
    ex.add_argument("--name", required=True)
    ex.add_argument("--turns", type=int, required=True)
    ex.set_defaults(func=cmd_extend)

    st = sub.add_parser("status")
    st.add_argument("--chat", default=None)
    st.set_defaults(func=cmd_status)

    t = sub.add_parser("tail")
    t.add_argument("--chat", default=None)
    t.add_argument("--follow", action="store_true")
    t.set_defaults(func=cmd_tail)

    h = sub.add_parser("hook")
    h.add_argument("harness")
    h.add_argument("event")
    h.set_defaults(func=cmd_hook)

    wk = sub.add_parser("waker")
    wk.add_argument("harness")
    wk.add_argument("--chat", required=True)
    wk.add_argument("--name", required=True)
    wk.add_argument("--thread", required=True)
    wk.set_defaults(func=cmd_waker)

    return p


def main():
    args = build_parser().parse_args()
    home = get_home()
    # hook relay must be able to tell "no DB file yet" apart from "unbound session",
    # so it opens (and thus creates) the db itself, only once a bound session is possible.
    conn = None if args.command == "hook" else get_db(home)
    return args.func(args, home, conn)


if __name__ == "__main__":
    sys.exit(main())
