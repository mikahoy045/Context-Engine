# Easy K3s Install Guide

This guide explains how to quickly deploy Context Engine on a single-node VPS using K3s (lightweight Kubernetes) and connect it to your local VS Code.

## Prerequisites

- Linux VPS with at least 4GB RAM and 20GB disk space
- Docker installed (for building images)
- Root or sudo access

## 1. Quick Install

Log into your server (SSH) and run these commands:

```bash
# Clone the repository
git clone https://github.com/m1rl0k/Context-Engine.git
cd Context-Engine/deploy/k3s-overlay

# Run the easy install script
chmod +x easy_install.sh build_k3s_images.sh
sudo ./easy_install.sh
```

**What this script does:**

* Installs **K3s** (Lightweight Kubernetes) if not already installed
* Builds and imports all Docker images into K3s containerd
* Deploys all services with single-node optimizations (RWO volumes, local-path storage)
* Enables **Remote Upload** feature for VS Code integration
* Outputs your connection URL

## 2. Connect VS Code

Once the script finishes, it will show a URL like `http://1.2.3.4:30810`.

1. Open **VS Code** locally.
2. Install the **Context Engine** extension.
3. Go to **Settings** (`Ctrl+,` or `Cmd+,`) and search for `Context Engine`.

**Required Settings:**
- **Endpoint**: `http://YOUR_SERVER_IP:30810` (replace with your actual server IP)
- **Scaffold Config**: `false` (prevents creating `ctx_config.json`, `.env`, `.mcp.json` in your projects)

**Optional Settings:**
- **Run On Startup**: Enable to auto-index when VS Code opens
- **Python Path**: Set to `python3` or your Python executable

The extension will connect to your remote K3s cluster's upload service on port **30810** (not 8004, which is for local Docker stacks).

## 3. Verify

1. Open any project in VS Code.
2. Open the Context Engine side panel.
3. Click the **Sync** button (cloud icon).
4. You should see "Uploading to remote..." followed by "Indexing...".

**If you see connection errors** (`Connection refused` on `localhost:8004`):
- Check that **Endpoint** is set to `http://YOUR_SERVER_IP:30810` (not localhost:8004)
- Verify the server IP is reachable: `curl http://YOUR_SERVER_IP:30810/health`
- Ensure no firewall is blocking port 30810

## 4. Clean Up Extension Files (Optional)

If you already ran the extension before disabling **Scaffold Config**, clean up project files:

```bash
# Remove from your project directories
rm ctx_config.json .env .mcp.json

# Add to .gitignore to prevent committing these files
echo -e "\n# Context Engine extension files\nctx_config.json\n.env\n.mcp.json" >> .gitignore
```

These files are unnecessary when using remote mode (K3s deployment).

## Configuration

The k3s-overlay deployment uses Kustomize patches to extend the base configuration with k3s-specific values:

- **Remote Upload**: Enabled by default (`REMOTE_UPLOAD_ENABLED: "1"`)
- **LlamaCPP Model**: Pre-configured with Granite 4.0 micro model
- **Storage**: Uses K3s local-path provisioner (RWO volumes)
- **Resources**: CPU/memory requests tuned for single-node
- **Volume Mounts**: Consolidated to avoid RWO conflicts

The base configuration from `deploy/kubernetes/configmap.yaml` is preserved. K3s-specific additions are applied via patches in `deploy/k3s-overlay/kustomization.yaml`.

## Troubleshooting

**Check Status:**

```bash
sudo k3s kubectl get pods -n context-engine
```

All pods should show "Running" and "1/1" ready.

**Check Logs:**

```bash
# Upload Service logs
sudo k3s kubectl logs -n context-engine -l component=upload-service -f

# Indexer logs
sudo k3s kubectl logs -n context-engine -l component=watcher -f

# Memory service logs
sudo k3s kubectl logs -n context-engine -l component=mcp-memory -f
```

**Common Issues:**

1. **Disk Space**: Ensure at least 20GB free. K3s requires disk usage below 85%.
   ```bash
   df -h /
   ```

2. **Images Not Found**: If pods show `ImagePullBackOff`, rebuild images:
   ```bash
   cd Context-Engine/deploy/k3s-overlay
   sudo ./build_k3s_images.sh
   ```

3. **Pod Stuck in ContainerCreating**: Check events and logs:
   ```bash
   sudo k3s kubectl describe pod <pod-name> -n context-engine
   sudo journalctl -u k3s --since "5 minutes ago" | tail -50
   ```

**Restarting Services:**

```bash
# Restart all deployments
sudo k3s kubectl rollout restart deployment -n context-engine

# Restart specific service
sudo k3s kubectl rollout restart deployment/mcp-memory -n context-engine
```

## Manual Image Building

If you need to rebuild images manually:

```bash
cd Context-Engine/deploy/k3s-overlay
sudo ./build_k3s_images.sh
```

This builds and imports:
- `context-engine-upload-service`
- `context-engine-indexer` / `context-engine-indexer-service`
- `context-engine-mcp` / `context-engine-memory`
- `context-engine-llamacpp`
