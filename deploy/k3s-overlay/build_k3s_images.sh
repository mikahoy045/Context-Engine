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

build_and_import() {
    IMAGE_NAME="$1"
    DOCKERFILE="$2"

    echo "------------------------------------------"
    echo "[+] Building $IMAGE_NAME from $DOCKERFILE..."
    docker build -t "$IMAGE_NAME:latest" -f "$DOCKERFILE" .
    
    echo "[+] Importing $IMAGE_NAME into K3s..."
    docker save "$IMAGE_NAME:latest" | sudo k3s ctr images import -
    echo "[+] Done: $IMAGE_NAME"
}

# 1. Upload Service
build_and_import "context-engine-upload-service" "Dockerfile.upload-service"

# 2. MCP Indexer (Watcher/Indexer)
build_and_import "context-engine-indexer" "Dockerfile.mcp-indexer"

# Tag as context-engine-indexer-service (required by indexer-services.yaml)
echo "[+] Tagging context-engine-indexer-service:latest..."
docker tag context-engine-indexer:latest context-engine-indexer-service:latest
docker save context-engine-indexer-service:latest | sudo k3s ctr images import -

# 3. Base/MCP (Memory)
# Note: Manifests might use context-engine-mcp or context-engine:latest
# Checking manifest mcp-memory.yaml usually uses context-engine-mcp or similar.
# Creating both tags to be safe if manifest references vary.
echo "------------------------------------------"
echo "[+] Building context-engine-mcp..."
docker build -t context-engine-mcp:latest -f Dockerfile.mcp .

echo "[+] Importing context-engine-mcp..."
docker save context-engine-mcp:latest | sudo k3s ctr images import -

# Tag as context-engine:latest (legacy)
echo "[+] Tagging context-engine:latest..."
docker tag context-engine-mcp:latest context-engine:latest
docker save context-engine:latest | sudo k3s ctr images import -

# Tag as context-engine-memory (required by mcp-memory deployments)
echo "[+] Tagging context-engine-memory:latest..."
docker tag context-engine-mcp:latest context-engine-memory:latest
docker save context-engine-memory:latest | sudo k3s ctr images import -

# 4. Llamacpp
echo "------------------------------------------"
echo "[+] Building context-engine-llamacpp from Dockerfile.llamacpp..."
docker build -t context-engine-llamacpp:latest -f Dockerfile.llamacpp .

echo "[+] Importing context-engine-llamacpp into K3s..."
docker save context-engine-llamacpp:latest | sudo k3s ctr images import -

# 5. Neo4j (for graph queries)
echo "------------------------------------------"
echo "[+] Building neo4j:5.26.0-community..."
docker pull neo4j:5.26.0-community

echo "[+] Importing neo4j into K3s..."
docker save neo4j:5.26.0-community | sudo k3s ctr images import -

echo "=========================================="
echo "All images built and imported!"
echo "=========================================="
