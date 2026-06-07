#!/bin/bash
# ORACLE DEPLOYMENT SCRIPT: Wave 2 (HTTPS & Gemini Integration)
# Run this on your Oracle Cloud Linux VM (144.24.117.114)

DOMAIN="marketradar-oracle.online"
API_KEY="INSERT_KEY_HERE"
EMAIL="vivekashokan007@gmail.com"  # Used for Certbot expiration notices

echo "Starting Oracle Relay Deployment for Wave 2 on Oracle Linux..."

# 1. Install dependencies (skipping full update to prevent OOM crash)
sudo dnf install -y oracle-epel-release-el8 || sudo dnf install -y epel-release
sudo dnf install -y python3 python3-pip nginx certbot python3-certbot-nginx policycoreutils-python-utils

# 2. Setup application directory
mkdir -p ~/market-relay
cd ~/market-relay

# 3. Setup Virtual Environment
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate

# 4. Install Dependencies
pip install fastapi uvicorn google-generativeai pydantic

# 5. Create .env file with the Gemini API Key
echo "GEMINI_API_KEY=${API_KEY}" > .env
chmod 600 .env

# 6. Configure Nginx Reverse Proxy
NGINX_CONF="/etc/nginx/conf.d/market-relay.conf"
sudo bash -c "cat > $NGINX_CONF" << EOF
server {
    listen 80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

# Allow Nginx to connect to the backend (SELinux)
sudo setsebool -P httpd_can_network_connect 1

# Test and restart Nginx
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl restart nginx

# Allow HTTP/HTTPS through firewalld if active
sudo firewall-cmd --permanent --add-service=http || true
sudo firewall-cmd --permanent --add-service=https || true
sudo firewall-cmd --reload || true

# 7. Run Certbot to get the free HTTPS Certificate
echo "Running Certbot to secure ${DOMAIN}..."
sudo certbot --nginx -n --agree-tos -d ${DOMAIN} -m ${EMAIL} --redirect

# 8. Create systemd service for the FastAPI app
SERVICE_FILE="/etc/systemd/system/market-relay.service"
sudo bash -c "cat > $SERVICE_FILE" << EOF
[Unit]
Description=Market Radar FastAPI Relay
After=network.target

[Service]
User=$USER
WorkingDirectory=$HOME/market-relay
EnvironmentFile=$HOME/market-relay/.env
ExecStart=$HOME/market-relay/venv/bin/uvicorn evaluator_app:app --host 127.0.0.1 --port 8443
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# 9. Reload systemd and start the service
sudo systemctl daemon-reload
sudo systemctl enable market-relay
sudo systemctl restart market-relay

echo "====================================================="
echo "Deployment finished."
echo "Your secure server is now running at: https://${DOMAIN}"
echo "Test the health endpoint: curl https://${DOMAIN}/health"
echo "====================================================="
