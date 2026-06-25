#!/bin/bash
# ====================================================================
# Deploy Azure Resource Manager Dashboard ke Azure VM
# Jalankan script ini di VM Azure (Ubuntu/Debian)
# ====================================================================

set -e

echo "========================================="
echo "  Azure Resource Manager - VM Deployment"
echo "========================================="

# 1. Update system
echo "[1/6] Updating system packages..."
sudo apt update && sudo apt upgrade -y

# 2. Install Python & pip
echo "[2/6] Installing Python..."
sudo apt install -y python3 python3-pip python3-venv nginx

# 3. Setup app directory
echo "[3/6] Setting up application..."
APP_DIR="/opt/azure-resource-manager"
sudo mkdir -p $APP_DIR
sudo cp -r . $APP_DIR/
cd $APP_DIR

# 4. Create virtual environment & install deps
echo "[4/6] Installing Python dependencies..."
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install gunicorn

# 5. Create systemd service
echo "[5/6] Creating systemd service..."
sudo tee /etc/systemd/system/azure-manager.service > /dev/null <<EOF
[Unit]
Description=Azure Resource Manager Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
Environment=PATH=$APP_DIR/venv/bin:/usr/bin
ExecStart=$APP_DIR/venv/bin/gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 300 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable azure-manager
sudo systemctl start azure-manager

# 6. Configure Nginx reverse proxy
echo "[6/6] Configuring Nginx..."
sudo tee /etc/nginx/sites-available/azure-manager > /dev/null <<'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/azure-manager /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx

echo ""
echo "========================================="
echo "  Deployment Complete!"
echo "========================================="
echo ""
echo "  Dashboard: http://$(hostname -I | awk '{print $1}')"
echo ""
echo "  Service commands:"
echo "    sudo systemctl status azure-manager"
echo "    sudo systemctl restart azure-manager"
echo "    sudo journalctl -u azure-manager -f"
echo ""
