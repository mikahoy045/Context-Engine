#!/bin/bash
set -e

# Build K3s Images Script
# Builds local Docker images and imports them into K3s/containerd

echo "=========================================="
echo "Context Engine - Build & Import Images"
echo "=========================================="

# Ensure we are in the root of the repo
# If script is in deploy/kubernetes, go up 2 levels
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
cd "$REPO_ROOT"
echo "[+] Working directory: $(pwd)"

import_to_k3s() {
    local IMAGE_NAME="$1"
    echo "[+] Importing $IMAGE_NAME into K3s..."
    
    local TMPFILE=$(mktemp /tmp/docker-image-XXXXXX.tar)
    docker save "$IMAGE_NAME:latest" -o "$TMPFILE"
    sudo k3s ctr images import "$TMPFILE"
    rm -f "$TMPFILE"
    
    if sudo k3s ctr images list | grep -q "docker.io/library/${IMAGE_NAME}:latest"; then
        echo "[+] Verified: $IMAGE_NAME imported successfully"
    else
        echo "[!] Warning: $IMAGE_NAME may not have imported correctly"
    fi
}

build_and_import() {
    IMAGE_NAME="$1"
    DOCKERFILE="$2"

    echo "------------------------------------------"
    echo "[+] Building $IMAGE_NAME from $DOCKERFILE..."
    docker build -t "$IMAGE_NAME:latest" -f "$DOCKERFILE" .
    
    import_to_k3s "$IMAGE_NAME"
    echo "[+] Done: $IMAGE_NAME"
}

# 1. Upload Service
build_and_import "context-engine-upload-service" "Dockerfile.upload-service"

# 2. MCP Indexer (Watcher/Indexer)
build_and_import "context-engine-indexer" "Dockerfile.mcp-indexer"

# Tag as context-engine-indexer-service (required by indexer-services.yaml)
echo "[+] Tagging context-engine-indexer-service:latest..."
docker tag context-engine-indexer:latest context-engine-indexer-service:latest
import_to_k3s "context-engine-indexer-service"

# 3. Base/MCP (Memory)
echo "------------------------------------------"
echo "[+] Building context-engine-mcp..."
docker build -t context-engine-mcp:latest -f Dockerfile.mcp .
import_to_k3s "context-engine-mcp"

# Tag as context-engine:latest (legacy)
echo "[+] Tagging context-engine:latest..."
docker tag context-engine-mcp:latest context-engine:latest
import_to_k3s "context-engine"

# Tag as context-engine-memory (required by mcp-memory deployments)
echo "[+] Tagging context-engine-memory:latest..."
docker tag context-engine-mcp:latest context-engine-memory:latest
import_to_k3s "context-engine-memory"

# 4. Llamacpp
echo "------------------------------------------"
echo "[+] Building context-engine-llamacpp from Dockerfile.llamacpp..."
docker build -t context-engine-llamacpp:latest -f Dockerfile.llamacpp .
import_to_k3s "context-engine-llamacpp"

# 5. Neo4j (for graph queries)
echo "------------------------------------------"
echo "[+] Pulling neo4j:4.4.28-community..."
docker pull neo4j:4.4.28-community

echo "[+] Importing neo4j into K3s..."
TMPFILE=$(mktemp /tmp/docker-image-XXXXXX.tar)
docker save neo4j:4.4.28-community -o "$TMPFILE"
sudo k3s ctr images import "$TMPFILE"
rm -f "$TMPFILE"

echo "=========================================="
echo "All images built and imported!"
echo "=========================================="
