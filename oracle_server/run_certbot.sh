#!/bin/bash
set -x
sudo firewall-cmd --permanent --add-service=https || true
sudo firewall-cmd --permanent --add-port=443/tcp || true
sudo firewall-cmd --reload || true
sudo certbot --nginx -d marketradar-oracle.online --non-interactive --agree-tos -m admin@marketradar-oracle.online --no-eff-email
