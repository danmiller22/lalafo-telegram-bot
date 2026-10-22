#!/data/data/com.termux/files/usr/bin/bash
set -eu

ROOT="$HOME/.lalafo-autoreply"
REPO_RAW="https://raw.githubusercontent.com/danmiller22/lalafo-telegram-bot/main"

echo "Installing the lightweight Lalafo responder..."
command -v python >/dev/null || { echo "Python is not installed"; exit 1; }
command -v curl >/dev/null || { echo "curl is not installed"; exit 1; }
mkdir -p "$ROOT" "$HOME/.termux/boot"
curl -fsSL "$REPO_RAW/termux/lalafo_autoreply.py" -o "$ROOT/worker.py"
chmod 700 "$ROOT" "$ROOT/worker.py"

printf "Lalafo phone or email: "
IFS= read -r LALAFO_LOGIN
printf "Lalafo password (input is hidden): "
IFS= read -r -s LALAFO_PASSWORD
printf "\n"
export LALAFO_LOGIN LALAFO_PASSWORD ROOT
python - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ["ROOT"])
path = root / "config.json"
path.write_text(json.dumps({
    "login": os.environ["LALAFO_LOGIN"].strip(),
    "password": os.environ["LALAFO_PASSWORD"],
}, ensure_ascii=False))
path.chmod(0o600)
PY
unset LALAFO_LOGIN LALAFO_PASSWORD

cat > "$ROOT/start.sh" <<'SH'
#!/data/data/com.termux/files/usr/bin/bash
ROOT="$HOME/.lalafo-autoreply"
if [ -f "$ROOT/worker.pid" ] && kill -0 "$(cat "$ROOT/worker.pid")" 2>/dev/null; then
  exit 0
fi
nohup python "$ROOT/worker.py" --config "$ROOT/config.json" >> "$ROOT/worker.log" 2>&1 &
echo $! > "$ROOT/worker.pid"
SH
chmod 700 "$ROOT/start.sh"

cat > "$HOME/.termux/boot/start-lalafo-autoreply" <<'SH'
#!/data/data/com.termux/files/usr/bin/bash
sleep 20
"$HOME/.lalafo-autoreply/start.sh"
SH
chmod 700 "$HOME/.termux/boot/start-lalafo-autoreply"

"$ROOT/start.sh"
sleep 5
echo
echo "INSTALLATION_COMPLETE"
echo "Log: tail -f $ROOT/worker.log"
tail -n 8 "$ROOT/worker.log" 2>/dev/null || true
