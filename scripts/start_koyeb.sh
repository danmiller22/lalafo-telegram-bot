#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${TS_AUTHKEY:-}" && -n "${TS_EXIT_NODE:-}" ]]; then
  python3 scripts/bootstrap_tailscale.py
  ts_dir=/tmp/tailscale-bin
  ts_socket=/tmp/tailscaled.sock
  "$ts_dir/tailscaled" \
    --tun=userspace-networking \
    --socks5-server=127.0.0.1:1055 \
    --state=/tmp/tailscaled.state \
    --socket="$ts_socket" \
    >/tmp/tailscaled.log 2>&1 &
  for _ in $(seq 1 30); do
    [[ -S "$ts_socket" ]] && break
    sleep 1
  done
  [[ -S "$ts_socket" ]]
  "$ts_dir/tailscale" --socket="$ts_socket" up \
    --auth-key="$TS_AUTHKEY" \
    --hostname=koyeb-lalafo \
    --exit-node="$TS_EXIT_NODE" \
    --exit-node-allow-lan-access=false \
    --accept-dns=false
  export LALAFO_CHAT_PROXY_URL=socks5://127.0.0.1:1055
fi

alembic upgrade head
exec uvicorn app.web:app --host 0.0.0.0 --port "${PORT:-8000}"
