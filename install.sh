#!/usr/bin/env bash
#
# c2c installer — links the CLI + skill and (optionally) wires the Stop hook.
#
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN="$REPO/bin/c2c"
SKILLS_DIR="$HOME/.claude/skills"
LOCAL_BIN="$HOME/.local/bin"
SETTINGS="$HOME/.claude/settings.json"

echo "c2c installer"
echo "  repo: $REPO"

# 1) CLI on PATH ------------------------------------------------------------
mkdir -p "$LOCAL_BIN"
ln -sf "$BIN" "$LOCAL_BIN/c2c"
chmod +x "$REPO/bin/c2c" "$REPO/bin/c2c_mail.py"
echo "  linked $LOCAL_BIN/c2c -> $BIN"
case ":$PATH:" in
  *":$LOCAL_BIN:"*) : ;;
  *) echo "  ! $LOCAL_BIN is not on your PATH — add it to use 'c2c' directly" ;;
esac

# 2) Skill discovery --------------------------------------------------------
mkdir -p "$SKILLS_DIR"
if [ -e "$SKILLS_DIR/c2c" ] && [ ! -L "$SKILLS_DIR/c2c" ]; then
  echo "  ! $SKILLS_DIR/c2c exists and is not a symlink — skipping skill link"
else
  ln -sfn "$REPO" "$SKILLS_DIR/c2c"
  echo "  linked $SKILLS_DIR/c2c -> $REPO"
fi

# 3) Stop hook (opt-in) -----------------------------------------------------
echo
read -r -p "Register the c2c Stop hook in ~/.claude/settings.json? [y/N] " ans
if [ "${ans:-N}" = "y" ] || [ "${ans:-N}" = "Y" ]; then
  [ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
  cp "$SETTINGS" "$SETTINGS.pre-c2c-bak"
  HOOK_CMD="$LOCAL_BIN/c2c stop-hook" python3 - "$SETTINGS" <<'PY'
import json, os, sys
p = sys.argv[1]
d = json.load(open(p))
cmd = os.environ["HOOK_CMD"]
stop = d.setdefault("hooks", {}).setdefault("Stop", [])
if not any("c2c stop-hook" in h.get("command", "") for e in stop for h in e.get("hooks", [])):
    stop.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
    json.dump(d, open(p, "w"), indent=2, ensure_ascii=False)
    print("  registered Stop hook (backup: %s.pre-c2c-bak)" % p)
else:
    print("  Stop hook already present (idempotent)")
PY
  echo "  note: only instances STARTED AFTER this will deliver mailbox messages."
else
  echo "  skipped — add it later, see README."
fi

echo
echo "Done. Try:  c2c whoami  &&  c2c list"
