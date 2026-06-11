#!/bin/bash
set -euo pipefail

SERVICE_NAME="market-relay"
APP_DIR="${HOME}/market-relay"
DOMAIN="https://marketradar-oracle.online"

echo "=== systemd unit ==="
systemctl cat "${SERVICE_NAME}" || true

echo
echo "=== app dir ==="
ls -la "${APP_DIR}" || true

echo
echo "=== env file (redacted) ==="
if [ -f "${APP_DIR}/.env" ]; then
  sed -E \
    -e 's/^(GEMINI_API_KEY=).+/\1<redacted>/' \
    -e 's/^(SUPABASE_ANON_KEY=).+/\1<redacted>/' \
    "${APP_DIR}/.env"
else
  echo "missing: ${APP_DIR}/.env"
fi

echo
echo "=== deployed app head ==="
sed -n '1,220p' "${APP_DIR}/evaluator_app.py" || true

echo
echo "=== service logs ==="
journalctl -u "${SERVICE_NAME}" -n 200 --no-pager || true

echo
echo "=== public health ==="
curl -s "${DOMAIN}/health" || true
echo

echo
echo "=== public openapi ==="
curl -s "${DOMAIN}/openapi.json" | head -c 2000 || true
echo
