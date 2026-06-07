#!/bin/bash
set -e

# 1. Flush the previous NAT port forward so Nginx can bind to Port 80
sudo iptables -t nat -F PREROUTING || true
sudo firewall-cmd --permanent --remove-forward-port=port=80:proto=tcp:toport=8000 || true
sudo firewall-cmd --reload || true

# 2. Install EPEL, Nginx, Certbot
sudo dnf install -y oracle-epel-release-el8 || sudo dnf install -y oracle-epel-release-el9 || sudo dnf install -y epel-release || true
sudo dnf install -y nginx certbot python3-certbot-nginx

# 3. Restart FastAPI with the New Key
sudo pkill -f uvicorn || true
export GEMINI_API_KEY="INSERT_KEY_HERE"
cd ~/oracle_server
nohup python3 -m uvicorn evaluator_app:app --host 127.0.0.1 --port 8000 > server.log 2>&1 &

# 4. Configure Nginx
cat << 'EOF' | sudo tee /etc/nginx/conf.d/marketradar.conf
server {
    listen 80;
    server_name marketradar-oracle.online;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

# Restart Nginx
sudo systemctl enable nginx
sudo systemctl restart nginx

# 5. Get Let's Encrypt Certificate
sudo certbot --nginx -d marketradar-oracle.online --non-interactive --agree-tos -m admin@marketradar-oracle.online

echo "HTTPS Deployment Complete"
