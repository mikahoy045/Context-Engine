#!/bin/bash
set -e

# Context Engine - Easy Install Script for K3s
# Usage: sudo ./easy_install.sh [--no-neo4j] [--glm <api-key>]

# Parse arguments
NEO4J_ENABLED="true"
GLM_API_KEY=""
REFRAG_MODE="llamacpp"

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-neo4j)
            NEO4J_ENABLED="false"
            shift
            ;;
        --glm)
            GLM_API_KEY="$2"
            REFRAG_MODE="glm"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: sudo ./easy_install.sh [--no-neo4j] [--glm <api-key>]"
            exit 1
            ;;
    esac
done

echo "=========================================="
echo "Context Engine - Easy Installer"
echo "=========================================="

if [ "$NEO4J_ENABLED" = "true" ]; then
    echo "[+] Neo4j: ENABLED"
else
    echo "[+] Neo4j: DISABLED"
fi

if [ "$REFRAG_MODE" = "glm" ]; then
    echo "[+] ReFrAg Runtime: GLM (cloud API)"
    echo "[+] GLM API Key: ${GLM_API_KEY:0:8}..."
else
    echo "[+] ReFrAg Runtime: llama.cpp (local, FREE)"
fi

echo ""
echo "[+] This installer will automatically:"
echo "    - Build and import Docker images into K3s"
echo "    - Deploy all services with optimized settings"
echo "    - Enable ReFrAg micro-chunking (16-token chunks)"
echo "    - Fix common PVC permission issues"
echo "    - Configure Neo4j graph database (if enabled)"
echo "    - Set up ReFrAg runtime (llama.cpp or GLM)"

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

    # Fix: Tag mcp image as indexer to prevent ErrImageNeverPull errors
    echo "[+] Fixing image tags for indexer deployments..."
    if sudo k3s ctr images list | grep -q "docker.io/library/context-engine:latest"; then
        if ! sudo k3s ctr images list | grep -q "docker.io/library/context-engine-indexer:latest"; then
            sudo k3s ctr images tag docker.io/library/context-engine:latest docker.io/library/context-engine-indexer:latest 2>/dev/null || true
        fi
        if ! sudo k3s ctr images list | grep -q "docker.io/library/context-engine-indexer-service:latest"; then
            sudo k3s ctr images tag docker.io/library/context-engine:latest docker.io/library/context-engine-indexer-service:latest 2>/dev/null || true
        fi
        echo "[+] Image tags fixed."
    fi
else
    echo "[!] Warning: Docker not found. Cannot build images locally."
    echo "    If pods fail with ImagePullBackOff, you need to install docker and run build_k3s_images.sh"
fi

# 4. Apply Manifests with K3s Patches
echo "[+] Deploying Context Engine (with K3s overrides)..."
sudo k3s kubectl apply -k "$(dirname "$0")"

# 5. Apply Neo4j Overlay (if enabled)
if [ "$NEO4J_ENABLED" = "true" ]; then
    echo "[+] Applying Neo4j overlay..."
    sudo k3s kubectl apply -k "$(dirname "$0")/neo4j-overlay/"
else
    echo "[+] Skipping Neo4j overlay (--no-neo4j specified)"
fi

# 6. Apply GLM Overlay (if --glm specified)
if [ "$REFRAG_MODE" = "glm" ]; then
    echo "[+] Applying GLM overlay with API key..."

    # Create secret with GLM API key
    sudo k3s kubectl create secret generic context-engine-glm-api-key \
        --from-literal=GLM_API_KEY="$GLM_API_KEY" \
        --namespace=context-engine \
        --dry-run=client -o yaml | sudo k3s kubectl apply -f -

    # Patch ConfigMap to use GLM runtime
    sudo k3s kubectl patch configmap context-engine-config -n context-engine \
        --type merge \
        -p "{\"data\": {\"REFRAG_RUNTIME\": \"glm\"}}"

    # Patch mcp-indexer deployment to inject GLM_API_KEY
    sudo k3s kubectl patch deployment mcp-indexer -n context-engine \
        --type='json' \
        -p='[{"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "GLM_API_KEY", "valueFrom": {"secretKeyRef": {"name": "context-engine-glm-api-key", "key": "GLM_API_KEY"}}}}]'

    # Patch mcp-indexer-http deployment to inject GLM_API_KEY
    sudo k3s kubectl patch deployment mcp-indexer-http -n context-engine \
        --type='json' \
        -p='[{"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "GLM_API_KEY", "valueFrom": {"secretKeyRef": {"name": "context-engine-glm-api-key", "key": "GLM_API_KEY"}}}}]'

    # Restart indexer to pick up new config
    echo "[+] Restarting indexer services..."
    sudo k3s kubectl rollout restart deployment/mcp-indexer -n context-engine
    sudo k3s kubectl rollout restart deployment/mcp-indexer-http -n context-engine

    echo "[+] GLM overlay applied successfully"
else
    echo "[+] Using default llama.cpp runtime (local, no API key needed)"
fi

# 7. Fix PVC Permission Issues (if pods fail to start)
echo "[+] Checking for PVC permission issues..."
sleep 10

# Check if any pods are stuck in Init:CrashLoopBackOff
FAILED_PODS=$(sudo k3s kubectl get pods -n context-engine --no-headers 2>/dev/null | grep -c "Init:CrashLoopBackOff" || true)

if [ "$FAILED_PODS" -gt 0 ]; then
    echo "[!] Detected pods with permission issues. Fixing PVCs..."

    # Remove finalizers from stuck PVCs
    for pvc in code-repos-pvc code-metadata-pvc code-models-pvc; do
        if sudo k3s kubectl get pvc "$pvc" -n context-engine &>/dev/null; then
            echo "[+] Removing finalizers from $pvc..."
            sudo k3s kubectl patch pvc "$pvc" -n context-engine \
                -p '{"metadata":{"finalizers":null}}' --type=merge 2>/dev/null || true
        fi
    done

    # Delete PVCs to force recreation with correct permissions
    echo "[+] Recreating PVCs with correct permissions..."
    sudo k3s kubectl delete pvc code-repos-pvc code-metadata-pvc code-models-pvc -n context-engine --timeout=60s 2>/dev/null || true

    # Wait for PVCs to be fully deleted
    echo "[+] Waiting for PVCs to be deleted..."
    for i in {1..30}; do
        if ! sudo k3s kubectl get pvc code-repos-pvc -n context-engine &>/dev/null; then
            break
        fi
        sleep 2
    done

    # Re-apply manifests to recreate PVCs
    echo "[+] Recreating PVCs..."
    sudo k3s kubectl apply -k "$(dirname "$0")" 2>/dev/null || true

    # Restart all affected deployments
    echo "[+] Restarting deployments to use new PVCs..."
    sudo k3s kubectl rollout restart deployment/mcp-indexer -n context-engine 2>/dev/null || true
    sudo k3s kubectl rollout restart deployment/mcp-indexer-http -n context-engine 2>/dev/null || true
    sudo k3s kubectl rollout restart deployment/mcp-memory-http -n context-engine 2>/dev/null || true
    sudo k3s kubectl rollout restart deployment/watcher -n context-engine 2>/dev/null || true
    sudo k3s kubectl rollout restart deployment/learning-reranker-worker -n context-engine 2>/dev/null || true

    echo "[+] PVCs fixed. Waiting for pods to be ready..."
    sleep 30
else
    echo "[+] No PVC permission issues detected."
fi

# 8. Wait for Services
echo "[+] Waiting for Upload Service to be ready..."
sudo k3s kubectl wait --for=condition=available --timeout=400s deployment/upload-service -n context-engine

if [ "$NEO4J_ENABLED" = "true" ]; then
    echo "[+] Waiting for Neo4j to be ready..."
    sudo k3s kubectl wait --for=condition=ready --timeout=300s statefulset/neo4j -n context-engine || echo "[!] Neo4j not ready (may still be starting)"
fi

# 9. Re-apply GLM configuration if needed (after PVC fix)
if [ "$REFRAG_MODE" = "glm" ]; then
    echo "[+] Verifying GLM configuration..."

    # Check if REFRAG_RUNTIME is set to glm in ConfigMap
    CURRENT_RUNTIME=$(sudo k3s kubectl get configmap context-engine-config -n context-engine -o jsonpath='{.data.REFRAG_RUNTIME}' 2>/dev/null || echo "")

    if [ "$CURRENT_RUNTIME" != "glm" ]; then
        echo "[!] Re-applying GLM configuration after PVC fix..."
        sudo k3s kubectl patch configmap context-engine-config -n context-engine \
            --type merge \
            -p '{"data": {"REFRAG_RUNTIME": "glm"}}'

        sudo k3s kubectl rollout restart deployment/mcp-indexer -n context-engine
        sudo k3s kubectl rollout restart deployment/mcp-indexer-http -n context-engine

        echo "[+] Waiting for indexer restart..."
        sleep 30
    else
        echo "[+] GLM configuration verified."
    fi
fi

# 10. Verify pods are running
echo "[+] Verifying deployment..."
sleep 15

RUNNING_PODS=$(sudo k3s kubectl get pods -n context-engine --no-headers 2>/dev/null | grep -c "Running" || echo "0")
TOTAL_PODS=$(sudo k3s kubectl get pods -n context-engine --no-headers 2>/dev/null | wc -l)

echo "[+] Pods running: $RUNNING_PODS/$TOTAL_PODS"

if [ "$RUNNING_PODS" -lt "$((TOTAL_PODS / 2))" ]; then
    echo "[!] Warning: Some pods may not be starting properly."
    echo "[!] Check pod status with: sudo k3s kubectl get pods -n context-engine"
    echo "[!] Check logs with: sudo k3s kubectl logs -n context-engine -l component=<component-name>"
fi

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
