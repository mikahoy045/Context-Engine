#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

from qdrant_client import QdrantClient
from watchdog.observers import Observer


logger = logging.getLogger(__name__)

# Health server state
_watcher_healthy = False
_watcher_started_at: Optional[str] = None


def _start_health_server() -> bool:
    """Start a lightweight HTTP health server in a background thread."""
    port = int(os.environ.get("WATCHER_HEALTH_PORT", "18004") or "18004")

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/readyz" or self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                payload = {
                    "ok": _watcher_healthy,
                    "app": "watcher",
                    "started_at": _watcher_started_at,
                }
                self.wfile.write(json.dumps(payload).encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args, **kwargs):
            # Quiet health server logs
            return

    try:
        srv = HTTPServer(("0.0.0.0", port), HealthHandler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        print(f"[health] Health server started on port {port}")
        return True
    except Exception as e:
        print(f"[health] Failed to start health server: {e}")
        return False
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.watch_index_core.config import (  # noqa: E402
    LOGGER,
    MODEL,
    QDRANT_URL,
    ROOT as WATCH_ROOT,
    default_collection_name,
)
from scripts.watch_index_core.utils import (
    get_boolean_env,
    resolve_vector_name_config,
    create_observer,
)
from scripts.watch_index_core.handler import IndexHandler  # noqa: E402
from scripts.watch_index_core.pseudo import _start_pseudo_backfill_worker  # noqa: E402
from scripts.watch_index_core.processor import _process_paths  # noqa: E402
from scripts.watch_index_core.queue import ChangeQueue  # noqa: E402
from scripts.workspace_state import (  # noqa: E402
    _extract_repo_name_from_path,
    compute_indexing_config_hash,
    get_collection_name,
    get_indexing_config_snapshot,
    is_multi_repo_mode,
    persist_indexing_config,
    update_indexing_status,
    update_workspace_state,
    initialize_watcher_state,
)

import scripts.ingest_code as idx  # noqa: E402

logger = LOGGER
ROOT = WATCH_ROOT
# Back-compat: legacy modules/tests expect a module-level COLLECTION constant.
# We use a sentinel and a getter to ensure the resolved value is returned.
_COLLECTION: Optional[str] = None


def get_collection() -> str:
    """Return the resolved collection name or the env default."""
    if _COLLECTION is not None:
        return _COLLECTION
    return default_collection_name()


def main() -> None:
    global _watcher_healthy, _watcher_started_at
    from datetime import datetime, timezone

    # Start health server first (even before full initialization)
    _start_health_server()
    _watcher_started_at = datetime.now(timezone.utc).isoformat()

    # Backend migration: detect file<->redis switch and migrate state if needed
    try:
        from scripts.workspace_state import detect_and_migrate_backend
        migrated = detect_and_migrate_backend(ROOT)
        if migrated is not None:
            print(f"[backend_migration] Migrated {migrated} items to new backend")
    except Exception as e:
        logger.warning(f"Backend migration check failed (continuing): {e}")

    # Resolve collection name from workspace state before any client/state ops
    try:
        from scripts.workspace_state import get_collection_name_with_staging as _get_coll
    except Exception:
        _get_coll = None

    multi_repo_enabled = False
    try:
        multi_repo_enabled = bool(is_multi_repo_mode())
    except Exception:
        multi_repo_enabled = False

    default_collection = default_collection_name()
    # In multi-repo mode, per-repo collections are resolved via _get_collection_for_file
    # and workspace_state; avoid deriving a root-level collection like "/work-<hash>".
    if _get_coll and not multi_repo_enabled:
        try:
            resolved = _get_coll(str(ROOT))
            if resolved:
                default_collection = resolved
        except Exception as e:
            logger.debug(f"Suppressed exception: {e}")
    if multi_repo_enabled:
        print("[multi_repo] Multi-repo mode enabled - per-repo collections in use")
    else:
        print("[single_repo] Single-repo mode enabled - using single collection")

    global _COLLECTION, COLLECTION
    _COLLECTION = default_collection
    COLLECTION = _COLLECTION

    print(
        f"Watch mode: root={ROOT} qdrant={QDRANT_URL} collection={default_collection} model={MODEL}"
    )

    # Health check: detect and auto-heal cache/collection sync issues.
    # In multi-repo mode this can be expensive and may duplicate external init checks,
    # so default it OFF unless explicitly enabled.
    run_health_check = get_boolean_env(
        "WATCH_COLLECTION_HEALTHCHECK",
        default=(False if multi_repo_enabled else True),
    )
    if run_health_check:
        try:
            from scripts.collection_health import auto_heal_if_needed, auto_heal_multi_repo

            print("[health_check] Checking collection health...")
            if multi_repo_enabled:
                # Multi-repo: check each repo's collection
                heal_result = auto_heal_multi_repo(str(ROOT), QDRANT_URL, dry_run=False)
                if heal_result.get("repos_healed", 0) > 0:
                    print(
                        f"[health_check] Cleared cache for {heal_result['repos_healed']} repos with empty collections"
                    )
                elif heal_result.get("repos_checked", 0) > 0:
                    print(f"[health_check] Checked {heal_result['repos_checked']} repos - all OK")
                else:
                    print("[health_check] No repos with cached state to check")
            else:
                # Single-repo mode
                heal_result = auto_heal_if_needed(
                    str(ROOT), default_collection, QDRANT_URL, dry_run=False
                )
                if heal_result.get("action_taken") == "cleared_cache":
                    print("[health_check] Cache cleared due to sync issue - files will be reindexed")
                elif not heal_result.get("health_check", {}).get("healthy", True):
                    print(
                        f"[health_check] Issue detected: {heal_result['health_check'].get('issue', 'unknown')}"
                    )
                else:
                    print("[health_check] Collection health OK")
        except Exception as e:
            print(f"[health_check] Warning: health check failed: {e}")

    client = QdrantClient(
        url=QDRANT_URL, timeout=int(os.environ.get("QDRANT_TIMEOUT", "20") or 20)
    )

    # Use centralized embedder factory if available (supports Qwen3 feature flag)
    try:
        from scripts.embedder import get_embedding_model, get_model_dimension

        model = get_embedding_model(MODEL)
        model_dim = get_model_dimension(MODEL)
    except ImportError:
        # Fallback to direct fastembed initialization
        from fastembed import TextEmbedding

        model = TextEmbedding(model_name=MODEL)
        model_dim = len(next(model.embed(["dimension probe"])))

    vector_name = resolve_vector_name_config(client, default_collection, model_dim, MODEL)

    # In multi-repo mode we index into per-repo collections; don't eagerly touch/create
    # the default collection unless explicitly enabled.
    ensure_default_collection = get_boolean_env(
        "WATCH_ENSURE_DEFAULT_COLLECTION",
        default=(False if multi_repo_enabled else True),
    )
    if ensure_default_collection:
        try:
            idx.ensure_collection_and_indexes_once(
                client, default_collection, model_dim, vector_name
            )
        except Exception as e:
            logger.debug(f"Suppressed exception: {e}")

        _start_pseudo_backfill_worker(client, default_collection, model_dim, vector_name)

    try:
        initialize_watcher_state(str(ROOT), multi_repo_enabled, default_collection)
    except Exception as e:
        print(f"[workspace_state] Error initializing workspace state: {e}")

    q = ChangeQueue(
        lambda paths: _process_paths(
            paths, client, model, vector_name, model_dim, str(ROOT)
        )
    )
    handler = IndexHandler(ROOT, q, client, default_collection)

    use_polling = get_boolean_env("WATCH_USE_POLLING")
    obs = create_observer(use_polling, observer_cls=Observer)
    obs.schedule(handler, str(ROOT), recursive=True)
    obs.start()

    # Perform initial file scan on startup
    print("[initial_scan] Scanning for files to index...")
    try:
        from pathlib import Path
        scanned_count = 0
        for root_dir, dirs, files in os.walk(str(ROOT)):
            for file in files:
                file_path = Path(root_dir) / file
                try:
                    rel = file_path.resolve().relative_to(ROOT.resolve())
                except ValueError:
                    continue
                if any(part == ".codebase" for part in rel.parts):
                    continue
                if not idx.is_indexable_file(file_path):
                    continue
                handler._maybe_enqueue(str(file_path))
                scanned_count += 1
        print(f"[initial_scan] Scanned {scanned_count} files for indexing")
    except Exception as e:
        print(f"[initial_scan] Error during initial scan: {e}")

    # Mark watcher as healthy after observer starts
    global _watcher_healthy
    _watcher_healthy = True
    print("[health] Watcher is now healthy and monitoring for changes")

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        _watcher_healthy = False
        obs.stop()
        obs.join()


if __name__ == "__main__":
    main()

# For legacy compatibility, provide a global COLLECTION variable. 
# Due to import binding, this will reflect the state at the end of the module execution.
COLLECTION = get_collection()
