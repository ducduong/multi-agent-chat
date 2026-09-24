#!/usr/bin/env bash
# Installs multi-agent-chat into ~/.multi-agent-chat and wires up hooks/symlinks
# in Claude Code, Codex and opencode. Honors $HOME so it can be pointed at a
# fake home directory for testing. Never touches ~/.multi-agent-chat/data.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAC_HOME="$HOME/.multi-agent-chat"

# Merges (install) or strips (uninstall) our hook entries into a harness's hook
# config, in place. $1=config path, $2=adapter json (source of our entries,
# ignored on uninstall), $3=real $HOME (substituted for the $HOME placeholder
# in the adapter file), $4=install|uninstall.
merge_hooks() {
  /usr/bin/python3 - "$1" "$2" "$3" "$4" <<'PYEOF'
import json
import os
import sys

settings_path, adapter_path, real_home, mode = sys.argv[1:5]

if not os.path.exists(settings_path):
    if mode == "uninstall":
        sys.exit(0)  # nothing to strip; don't create a file that never existed
    settings = {}
else:
    with open(settings_path, "r", encoding="utf-8") as f:
        settings = json.load(f)

# Both Claude Code's settings.json and Codex's hooks.json nest hook events
# under a "hooks" key, beside sibling keys such as "description".
def strip_ours(container):
    for event in list(container.keys()):
        groups = container[event]
        if not isinstance(groups, list):
            continue  # non-hook-event key (e.g. "audor", "description"): leave untouched
        kept_groups = []
        for g in groups:
            if not isinstance(g, dict) or "hooks" not in g:
                kept_groups.append(g)
                continue
            kept_hooks = [h for h in g["hooks"] if "multi-agent-chat" not in h.get("command", "")]
            if kept_hooks:
                g = dict(g)
                g["hooks"] = kept_hooks
                kept_groups.append(g)
            # else: this group was ours only -- drop it
        if kept_groups:
            container[event] = kept_groups
        else:
            del container[event]  # restores the original shape when we added this event

# One-time cleanup: an earlier version of this script put our entries directly
# at the top level of hooks.json instead of nesting them under "hooks". Strip
# those too so re-running the fixed installer repairs an already-installed file.
strip_ours(settings)

settings.setdefault("hooks", {})
container = settings["hooks"]
strip_ours(container)

if mode == "install":
    with open(adapter_path, "r", encoding="utf-8") as f:
        adapter_text = f.read().replace("$HOME", real_home)
    adapter_events = json.loads(adapter_text)
    for event, groups in adapter_events.items():
        container.setdefault(event, [])
        container[event].extend(groups)

if not settings["hooks"]:
    del settings["hooks"]  # restores the original shape when "hooks" didn't exist before

os.makedirs(os.path.dirname(settings_path), exist_ok=True)
with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)
    f.write("\n")
PYEOF
}

make_symlink() {
  local target="$1" link="$2"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    echo "error: $link already exists and is not a symlink; aborting" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$link")"
  ln -sfn "$target" "$link"
}

remove_our_symlink() {
  local link="$1"
  if [ -L "$link" ]; then
    case "$(readlink "$link")" in
      "$MAC_HOME"/*) rm -f "$link" ;;
    esac
  fi
}

uninstall() {
  merge_hooks "$HOME/.claude/settings.json" "$REPO_ROOT/adapters/claude/hooks.json" "$HOME" uninstall
  merge_hooks "$HOME/.codex/hooks.json" "$REPO_ROOT/adapters/codex/hooks.json" "$HOME" uninstall

  remove_our_symlink "$HOME/.claude/skills/multi-agent-chat"
  remove_our_symlink "$HOME/.codex/skills/multi-agent-chat"
  remove_our_symlink "$HOME/.config/opencode/skills/multi-agent-chat"
  remove_our_symlink "$HOME/.config/opencode/plugins/multi-agent-chat.js"

  echo "multi-agent-chat uninstalled. data/ and backups left under $MAC_HOME"
}

install() {
  mkdir -p "$MAC_HOME"

  local timestamp backup_dir
  timestamp="$(date +%Y%m%d%H%M%S)"
  backup_dir="$MAC_HOME/backup/$timestamp"
  if [ -f "$HOME/.claude/settings.json" ] || [ -f "$HOME/.codex/hooks.json" ]; then
    mkdir -p "$backup_dir"
    [ -f "$HOME/.claude/settings.json" ] && cp "$HOME/.claude/settings.json" "$backup_dir/claude-settings.json"
    [ -f "$HOME/.codex/hooks.json" ] && cp "$HOME/.codex/hooks.json" "$backup_dir/codex-hooks.json"
  fi

  rm -rf "$MAC_HOME/skill.new" "$MAC_HOME/adapters.new" "$MAC_HOME/skill.old" "$MAC_HOME/adapters.old"
  mkdir -p "$MAC_HOME/skill.new" "$MAC_HOME/adapters.new"
  cp -R "$REPO_ROOT/skill/." "$MAC_HOME/skill.new/"
  cp -R "$REPO_ROOT/adapters/." "$MAC_HOME/adapters.new/"

  # Copy-then-swap: skill/ is imported by a running chat via a symlink, so it
  # must never be observed missing mid-install.
  if [ -d "$MAC_HOME/skill" ]; then mv "$MAC_HOME/skill" "$MAC_HOME/skill.old"; fi
  if [ -d "$MAC_HOME/adapters" ]; then mv "$MAC_HOME/adapters" "$MAC_HOME/adapters.old"; fi
  mv "$MAC_HOME/skill.new" "$MAC_HOME/skill"
  mv "$MAC_HOME/adapters.new" "$MAC_HOME/adapters"
  rm -rf "$MAC_HOME/skill.old" "$MAC_HOME/adapters.old"

  if [ ! -f "$MAC_HOME/config.json" ]; then
    /usr/bin/python3 -c "
import json, sys
sys.path.insert(0, '$MAC_HOME/skill/multi-agent-chat/scripts')
from chat import DEFAULT_CONFIG
with open('$MAC_HOME/config.json', 'w') as f:
    json.dump(DEFAULT_CONFIG, f, indent=2)
    f.write('\n')
"
  fi

  make_symlink "$MAC_HOME/skill/multi-agent-chat" "$HOME/.claude/skills/multi-agent-chat"
  make_symlink "$MAC_HOME/skill/multi-agent-chat" "$HOME/.codex/skills/multi-agent-chat"
  make_symlink "$MAC_HOME/skill/multi-agent-chat" "$HOME/.config/opencode/skills/multi-agent-chat"
  make_symlink "$MAC_HOME/adapters/opencode/multi-agent-chat.js" "$HOME/.config/opencode/plugins/multi-agent-chat.js"

  merge_hooks "$HOME/.claude/settings.json" "$REPO_ROOT/adapters/claude/hooks.json" "$HOME" install
  merge_hooks "$HOME/.codex/hooks.json" "$REPO_ROOT/adapters/codex/hooks.json" "$HOME" install

  echo "multi-agent-chat installed into $MAC_HOME"
  echo "Codex: open codex once and run /hooks to trust the multi-agent-chat hooks"
}

if [ "${1:-}" = "--uninstall" ]; then
  uninstall
else
  install
fi
