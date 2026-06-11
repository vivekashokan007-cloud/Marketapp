#!/bin/bash
set -e
set -x

echo "=== Opening Firewalld Ports ==="
sudo firewall-cmd --permanent --add-port=80/tcp || true
sudo firewall-cmd --permanent --add-port=443/tcp || true
sudo firewall-cmd --reload || true

echo "=== Freeing up port 80 ==="
sudo pkill -f uvicorn || true
sudo pkill -f nginx || true
sudo pkill -f python || true
sleep 2

echo "=== Creating clean Virtual Environment ==="
cd ~/oracle_server
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/pip install python-dotenv supabase

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

echo "=== Installing Certbot via Pip ==="
venv/bin/pip install certbot
sudo ~/oracle_server/venv/bin/certbot certonly --standalone -d marketradar-oracle.online --non-interactive --agree-tos -m admin@marketradar-oracle.online

echo "=== Starting Uvicorn with HTTPS ==="
nohup sudo env \
  GEMINI_API_KEY="${GEMINI_API_KEY:-}" \
  SUPABASE_URL="${SUPABASE_URL:-}" \
  SUPABASE_ANON_KEY="${SUPABASE_ANON_KEY:-}" \
  ~/oracle_server/venv/bin/uvicorn evaluator_app:app --host 0.0.0.0 --port 443 --ssl-keyfile /etc/letsencrypt/live/marketradar-oracle.online/privkey.pem --ssl-certfile /etc/letsencrypt/live/marketradar-oracle.online/fullchain.pem > uvicorn.log 2>&1 &

echo "HTTPS Deployment Complete!"
