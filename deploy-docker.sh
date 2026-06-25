#!/bin/bash
# ====================================================================
# Deploy Azure Resource Manager via Docker di Azure VM
# ====================================================================
set -e

echo "========================================="
echo "  Docker Deployment - Azure Resource Mgr"
echo "========================================="

# 1. Copy files ke VM (jalankan dari local)
# scp -r . azureuser@<VM_IP>:/home/azureuser/azure-manager/

# 2. Jalankan di VM:
cd /home/azureuser/azure-manager

# Build & run
docker compose up -d --build

echo ""
echo "========================================="
echo "  Deployment Complete!"
echo "========================================="
echo "  Dashboard: http://$(hostname -I | awk '{print $1}')"
echo ""
echo "  Docker commands:"
echo "    docker compose logs -f"
echo "    docker compose restart"
echo "    docker compose down"
echo ""
