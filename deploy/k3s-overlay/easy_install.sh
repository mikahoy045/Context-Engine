#!/bin/bash
set -e

# Context Engine - Easy Install Script for K3s
# Usage: sudo ./easy_install.sh

echo "=========================================="
echo "Context Engine - Easy Installer"
echo "=========================================="

# 1. Install K3s if not present
if ! command -v k3s &> /dev/null; then
    echo "[+] Installing K3s (Lightweight Kubernetes)..."
    curl -sfL https://get.k3s.io | sh -
    echo "[+] K3s installed successfully."
else
    echo "[+] K3s is already installed."
fi

# Ensure we have kubectl access
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# 2. Create Namespace
sudo k3s kubectl create namespace context-engine --dry-run=client -o yaml | sudo k3s kubectl apply -f -

# 3.5 Build/Import Images (if Docker available)
if command -v docker &> /dev/null; then
    echo "[+] Docker found. Checking if images need to be built..."
    if ! sudo k3s ctr images list | grep -q "context-engine-upload-service"; then
        echo "[!] Context Engine images not found in K3s. Building them now (this will take time)..."
        "$(dirname "$0")/build_k3s_images.sh"
    else
        echo "[+] Images already present in K3s."
    fi
else
    echo "[!] Warning: Docker not found. Cannot build images locally."
    echo "    If pods fail with ImagePullBackOff, you need to install docker and run build_k3s_images.sh"
fi

# 4. Apply Manifests with K3s Patches
echo "[+] Deploying Context Engine (with K3s overrides)..."
sudo k3s kubectl apply -k "$(dirname "$0")"

# 5. Wait for Services
echo "[+] Waiting for Upload Service to be ready..."
sudo k3s kubectl wait --for=condition=available --timeout=400s deployment/upload-service -n context-engine

# 6. Get Access Info
NODE_IP=$(ip -4 addr show eth0 | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n 1)
if [ -z "$NODE_IP" ]; then
    NODE_IP="YOUR_SERVER_IP"
fi

echo ""
echo "=========================================="
echo "DEPLOYMENT COMPLETE!"
echo "=========================================="
echo ""
echo "Your Context Engine is running."
echo ""
echo "Remote Upload URL: http://$NODE_IP:30810"
echo ""
echo "VS Code Extension Setup:"
echo "1. Settings > Context Engine > Remote"
echo "2. URL: http://$NODE_IP:30810"
echo "3. Enabled: Checked"
echo ""
echo "To check logs: sudo k3s kubectl logs -n context-engine -l component=upload-service -f"
echo "=========================================="
