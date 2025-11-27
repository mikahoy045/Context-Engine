#!/usr/bin/env python3
"""
Standalone Remote Upload Client for Context-Engine.

This is a self-contained version of the remote upload client that doesn't require
the full Context-Engine repository. It includes only the essential functions
needed for delta bundle creation and upload.

Example usage:
    python3 standalone_upload_client.py --path /path/to/your/project --server https://your-server.com
"""

import os
import json
import time
import uuid
import hashlib
import tarfile
import tempfile
import logging
import argparse
from pathlib import Path, PurePosixPath
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =============================================================================
# EMBEDDED DEPENDENCIES (Extracted from Context-Engine)
# =============================================================================

# Language detection mapping (from ingest_code.py)
CODE_EXTS = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "shell",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".psd1": "powershell",
    ".sql": "sql",
    ".md": "markdown",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".xml": "xml",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".less": "less",
    ".json": "json",
    "Dockerfile": "dockerfile",
    "Makefile": "makefile",
    ".tf": "terraform",
    ".tfvars": "terraform",
    ".hcl": "terraform",
    ".vue": "vue",
    ".svelte": "svelte",
    ".elm": "elm",
    ".dart": "dart",
    ".lua": "lua",
    ".r": "r",
    ".R": "r",
    ".m": "matlab",
    ".pl": "perl",
    ".swift": "swift",
    ".kt": "kotlin",
    ".cljs": "clojure",
    ".clj": "clojure",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".zig": "zig",
    ".nim": "nim",
    ".v": "verilog",
    ".sv": "verilog",
    ".vhdl": "vhdl",
    ".asm": "assembly",
    ".s": "assembly",
    ". Dockerfile": "dockerfile",
}

def hash_id(text: str, path: str, start: int, end: int) -> str:
    """Generate hash ID for content (from ingest_code.py)."""
    h = hashlib.sha1(
        f"{path}:{start}-{end}\n{text}".encode("utf-8", errors="ignore")
    ).hexdigest()
    return h[:16]

def get_collection_name(repo_name: Optional[str] = None) -> str:
    """Generate collection name with 8-char hash for local workspaces.

    Simplified version from workspace_state.py.
    """
    if not repo_name:
        return "default-collection"
    hash_obj = hashlib.sha256(repo_name.encode())
    short_hash = hash_obj.hexdigest()[:8]
    return f"{repo_name}-{short_hash}"

def _extract_repo_name_from_path(workspace_path: str) -> str:
    """Extract repository name from workspace path.

    Simplified version from workspace_state.py.
    """
    try:
        path = Path(workspace_path).resolve()
        # Get the directory name as repo name
        return path.name
    except Exception:
        return "unknown-repo"

# Simple file-based hash cache (simplified from workspace_state.py)
class SimpleHashCache:
    """Simple file-based hash cache for tracking file changes."""

    def __init__(self, workspace_path: str, repo_name: str):
        self.workspace_path = Path(workspace_path).resolve()
        self.repo_name = repo_name
        self.cache_dir = self.workspace_path / ".context-engine"
        self.cache_file = self.cache_dir / "file_cache.json"
        self.cache_dir.mkdir(exist_ok=True)

    def _load_cache(self) -> Dict[str, str]:
        """Load cache from disk."""
        if not self.cache_file.exists():
            return {}
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get("file_hashes", {})
        except Exception:
            return {}

    def _save_cache(self, file_hashes: Dict[str, str]):
        """Save cache to disk."""
        try:
            data = {
                "file_hashes": file_hashes,
                "updated_at": datetime.now().isoformat()
            }
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def get_hash(self, file_path: str) -> str:
        """Get cached file hash."""
        file_hashes = self._load_cache()
        abs_path = str(Path(file_path).resolve())
        return file_hashes.get(abs_path, "")

    def set_hash(self, file_path: str, file_hash: str):
        """Set cached file hash."""
        file_hashes = self._load_cache()
        abs_path = str(Path(file_path).resolve())
        file_hashes[abs_path] = file_hash
        self._save_cache(file_hashes)

    def all_paths(self) -> List[str]:
        """Return all cached absolute file paths."""
        file_hashes = self._load_cache()
        return list(file_hashes.keys())

    def remove_hash(self, file_path: str) -> None:
        """Remove a cached file hash if present."""
        file_hashes = self._load_cache()
        abs_path = str(Path(file_path).resolve())
        if abs_path in file_hashes:
            file_hashes.pop(abs_path, None)
            self._save_cache(file_hashes)

# Create global cache instance (will be initialized in RemoteUploadClient)
_hash_cache: Optional[SimpleHashCache] = None

def get_cached_file_hash(file_path: str, repo_name: Optional[str] = None) -> str:
    """Get cached file hash for tracking changes."""
    global _hash_cache
    if _hash_cache:
        return _hash_cache.get_hash(file_path)
    return ""

def set_cached_file_hash(file_path: str, file_hash: str, repo_name: Optional[str] = None):
    """Set cached file hash for tracking changes."""
    global _hash_cache
    if _hash_cache:
        _hash_cache.set_hash(file_path, file_hash)


def get_all_cached_paths(repo_name: Optional[str] = None) -> List[str]:
    """Return all tracked file paths from the local cache.

    The repo_name parameter is accepted for API symmetry with the non-standalone
    client but is not used here, since this cache is always per-workspace.
    """
    global _hash_cache
    if _hash_cache:
        return _hash_cache.all_paths()
    return []


def remove_cached_file(file_path: str, repo_name: Optional[str] = None) -> None:
    """Remove a file entry from the local cache if present."""
    global _hash_cache
    if _hash_cache:
        _hash_cache.remove_hash(file_path)


class RemoteUploadClient:
    """Client for uploading delta bundles to remote server."""

    def _translate_to_container_path(self, host_path: str) -> str:
        """Translate host path to container path for API communication."""
        host_root = (os.environ.get("HOST_ROOT", "") or "/home/coder/project/Context-Engine/dev-workspace").strip()
        container_root = (os.environ.get("CONTAINER_ROOT", "/work") or "/work").strip()

        host_path_obj = Path(host_path)
        if host_root:
            try:
                host_root_obj = Path(host_root)
                relative = host_path_obj.relative_to(host_root_obj)
                container = PurePosixPath(container_root)
                if relative.parts:
                    container = container.joinpath(*relative.parts)
                return str(container)
            except ValueError:
                pass
            except Exception:
                pass

        # Fallback: strip drive/anchor and map to /work/<repo-name>
        try:
            container = PurePosixPath(container_root)
            usable_parts = [part for part in host_path_obj.parts if part not in (host_path_obj.anchor, host_path_obj.drive)]
            if usable_parts:
                repo_name = usable_parts[-1]
                return str(container.joinpath(repo_name))
        except Exception:
            pass

        return host_path.replace('\\', '/').replace(':', '')

    def __init__(self, upload_endpoint: str, workspace_path: str, collection_name: str,
                 max_retries: int = 3, timeout: int = 30, metadata_path: Optional[str] = None):
        """Initialize remote upload client."""
        self.upload_endpoint = upload_endpoint.rstrip('/')
        self.workspace_path = workspace_path
        self.collection_name = collection_name
        self.max_retries = max_retries
        self.timeout = timeout
        self.temp_dir = None

        # Set environment variables for cache functions
        os.environ["WORKSPACE_PATH"] = workspace_path

        # Store repo name and initialize hash cache
        self.repo_name = _extract_repo_name_from_path(workspace_path)
        # Fallback to directory name if repo detection fails (for non-git repos)
        if not self.repo_name:
            self.repo_name = Path(workspace_path).name
        global _hash_cache
        _hash_cache = SimpleHashCache(workspace_path, self.repo_name)

        # Setup HTTP session with simple retry
        self.session = requests.Session()
        retry_strategy = Retry(total=max_retries, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with cleanup."""
        self.cleanup()

    def cleanup(self):
        """Clean up temporary directories."""
        if self.temp_dir and os.path.exists(self.temp_dir):
            try:
                import shutil
                shutil.rmtree(self.temp_dir)
                logger.debug(f"[remote_upload] Cleaned up temporary directory: {self.temp_dir}")
            except Exception as e:
                logger.warning(f"[remote_upload] Failed to cleanup temp directory {self.temp_dir}: {e}")
            finally:
                self.temp_dir = None

    def get_mapping_summary(self) -> Dict[str, Any]:
        """Return derived collection mapping details."""
        container_path = self._translate_to_container_path(self.workspace_path)
        return {
            "repo_name": self.repo_name,
            "collection_name": self.collection_name,
            "source_path": self.workspace_path,
            "container_path": container_path,
            "upload_endpoint": self.upload_endpoint,
        }

    def log_mapping_summary(self) -> None:
        """Log mapping summary for user visibility."""
        info = self.get_mapping_summary()
        logger.info("[remote_upload] Collection mapping:")
        logger.info(f"  repo_name: {info['repo_name']}")
        logger.info(f"  collection_name: {info['collection_name']}")
        logger.info(f"  source_path: {info['source_path']}")
        logger.info(f"  container_path: {info['container_path']}")

    def _get_temp_bundle_dir(self) -> Path:
        """Get or create temporary directory for bundle creation."""
        if not self.temp_dir:
            self.temp_dir = tempfile.mkdtemp(prefix="delta_bundle_")
        return Path(self.temp_dir)
    # CLI is stateless - sequence tracking is handled by server

    def detect_file_changes(self, changed_paths: List[Path]) -> Dict[str, List]:
        """
        Detect what type of changes occurred for each file path.

        Args:
            changed_paths: List of changed file paths

        Returns:
            Dictionary with change types: created, updated, deleted, moved, unchanged
        """
        changes = {
            "created": [],
            "updated": [],
            "deleted": [],
            "moved": [],
            "unchanged": []
        }

        for path in changed_paths:
            abs_path = str(path.resolve())
            cached_hash = get_cached_file_hash(abs_path, self.repo_name)

            if not path.exists():
                # File was deleted
                if cached_hash:
                    changes["deleted"].append(path)
            else:
                # File exists - calculate current hash
                try:
                    with open(path, 'rb') as f:
                        content = f.read()
                    current_hash = hashlib.sha1(content).hexdigest()

                    if not cached_hash:
                        # New file
                        changes["created"].append(path)
                    elif cached_hash != current_hash:
                        # Modified file
                        changes["updated"].append(path)
                    else:
                        # Unchanged (might be a move detection candidate)
                        changes["unchanged"].append(path)

                    # Update cache
                    set_cached_file_hash(abs_path, current_hash, self.repo_name)
                except Exception:
                    # Skip files that can't be read
                    continue

        # Detect moves by looking for files with same content hash
        # but different paths (requires additional tracking)
        changes["moved"] = self._detect_moves(changes["created"], changes["deleted"])

        return changes

    def _detect_moves(self, created_files: List[Path], deleted_files: List[Path]) -> List[Tuple[Path, Path]]:
        """
        Detect file moves by matching content hashes between created and deleted files.

        Args:
            created_files: List of newly created files
            deleted_files: List of deleted files

        Returns:
            List of (source, destination) path tuples for detected moves
        """
        moves = []
        deleted_hashes = {}

        # Build hash map for deleted files
        for deleted_path in deleted_files:
            try:
                # Try to get cached hash first, fallback to file content
                cached_hash = get_cached_file_hash(str(deleted_path), self.repo_name)
                if cached_hash:
                    deleted_hashes[cached_hash] = deleted_path
                    continue

                # If no cached hash, try to read from file if it still exists
                if deleted_path.exists():
                    with open(deleted_path, 'rb') as f:
                        content = f.read()
                    file_hash = hashlib.sha1(content).hexdigest()
                    deleted_hashes[file_hash] = deleted_path
            except Exception:
                continue

        # Match created files with deleted files by hash
        for created_path in created_files:
            try:
                with open(created_path, 'rb') as f:
                    content = f.read()
                file_hash = hashlib.sha1(content).hexdigest()

                if file_hash in deleted_hashes:
                    source_path = deleted_hashes[file_hash]
                    moves.append((source_path, created_path))
                    # Remove from consideration
                    del deleted_hashes[file_hash]
            except Exception:
                continue

        return moves

    def create_delta_bundle(self, changes: Dict[str, List]) -> Tuple[str, Dict[str, Any]]:
        """
        Create a delta bundle from detected changes.

        Args:
            changes: Dictionary of file changes by type

        Returns:
            Tuple of (bundle_path, manifest_metadata)
        """
        bundle_id = str(uuid.uuid4())
        # CLI is stateless - server handles sequence numbers
        created_at = datetime.now().isoformat()

        # Create temporary directory for bundle
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create directory structure
            files_dir = temp_path / "files"
            metadata_dir = temp_path / "metadata"
            files_dir.mkdir()
            metadata_dir.mkdir()

            # Create subdirectories
            (files_dir / "created").mkdir()
            (files_dir / "updated").mkdir()
            (files_dir / "moved").mkdir()

            operations = []
            total_size = 0
            file_hashes = {}

            # Process created files
            for path in changes["created"]:
                rel_path = str(path.relative_to(Path(self.workspace_path)))
                try:
                    with open(path, 'rb') as f:
                        content = f.read()
                    file_hash = hashlib.sha1(content).hexdigest()
                    content_hash = f"sha1:{file_hash}"

                    # Write file to bundle
                    bundle_file_path = files_dir / "created" / rel_path
                    bundle_file_path.parent.mkdir(parents=True, exist_ok=True)
                    bundle_file_path.write_bytes(content)

                    # Get file info
                    stat = path.stat()
                    language = CODE_EXTS.get(path.suffix.lower(), "unknown")

                    operation = {
                        "operation": "created",
                        "path": rel_path,
                        "relative_path": rel_path,
                        "absolute_path": str(path.resolve()),
                        "size_bytes": stat.st_size,
                        "content_hash": content_hash,
                        "file_hash": f"sha1:{hash_id(content.decode('utf-8', errors='ignore'), rel_path, 1, len(content.splitlines()))}",
                        "modified_time": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "language": language
                    }
                    operations.append(operation)
                    file_hashes[rel_path] = f"sha1:{file_hash}"
                    total_size += stat.st_size
                    set_cached_file_hash(str(path.resolve()), file_hash, self.repo_name)

                except Exception as e:
                    print(f"[bundle_create] Error processing created file {path}: {e}")
                    continue

            # Process updated files
            for path in changes["updated"]:
                rel_path = str(path.relative_to(Path(self.workspace_path)))
                try:
                    with open(path, 'rb') as f:
                        content = f.read()
                    file_hash = hashlib.sha1(content).hexdigest()
                    content_hash = f"sha1:{file_hash}"
                    previous_hash = get_cached_file_hash(str(path.resolve()), self.repo_name)

                    # Write file to bundle
                    bundle_file_path = files_dir / "updated" / rel_path
                    bundle_file_path.parent.mkdir(parents=True, exist_ok=True)
                    bundle_file_path.write_bytes(content)

                    # Get file info
                    stat = path.stat()
                    language = CODE_EXTS.get(path.suffix.lower(), "unknown")

                    operation = {
                        "operation": "updated",
                        "path": rel_path,
                        "relative_path": rel_path,
                        "absolute_path": str(path.resolve()),
                        "size_bytes": stat.st_size,
                        "content_hash": content_hash,
                        "previous_hash": f"sha1:{previous_hash}" if previous_hash else None,
                        "file_hash": f"sha1:{hash_id(content.decode('utf-8', errors='ignore'), rel_path, 1, len(content.splitlines()))}",
                        "modified_time": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "language": language
                    }
                    operations.append(operation)
                    file_hashes[rel_path] = f"sha1:{file_hash}"
                    total_size += stat.st_size
                    set_cached_file_hash(str(path.resolve()), file_hash, self.repo_name)

                except Exception as e:
                    print(f"[bundle_create] Error processing updated file {path}: {e}")
                    continue

            # Process moved files
            for source_path, dest_path in changes["moved"]:
                dest_rel_path = str(dest_path.relative_to(Path(self.workspace_path)))
                source_rel_path = str(source_path.relative_to(Path(self.workspace_path)))
                try:
                    with open(dest_path, 'rb') as f:
                        content = f.read()
                    file_hash = hashlib.sha1(content).hexdigest()
                    content_hash = f"sha1:{file_hash}"

                    # Write file to bundle
                    bundle_file_path = files_dir / "moved" / dest_rel_path
                    bundle_file_path.parent.mkdir(parents=True, exist_ok=True)
                    bundle_file_path.write_bytes(content)

                    # Get file info
                    stat = dest_path.stat()
                    language = CODE_EXTS.get(dest_path.suffix.lower(), "unknown")

                    operation = {
                        "operation": "moved",
                        "path": dest_rel_path,
                        "relative_path": dest_rel_path,
                        "absolute_path": str(dest_path.resolve()),
                        "source_path": source_rel_path,
                        "source_relative_path": source_rel_path,
                        "source_absolute_path": str(source_path.resolve()),
                        "size_bytes": stat.st_size,
                        "content_hash": content_hash,
                        "file_hash": f"sha1:{hash_id(content.decode('utf-8', errors='ignore'), dest_rel_path, 1, len(content.splitlines()))}",
                        "modified_time": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "language": language
                    }
                    operations.append(operation)
                    file_hashes[dest_rel_path] = f"sha1:{file_hash}"
                    total_size += stat.st_size
                    set_cached_file_hash(str(dest_path.resolve()), file_hash, self.repo_name)

                except Exception as e:
                    print(f"[bundle_create] Error processing moved file {source_path} -> {dest_path}: {e}")
                    continue

            # Process deleted files
            for path in changes["deleted"]:
                rel_path = str(path.relative_to(Path(self.workspace_path)))
                try:
                    previous_hash = get_cached_file_hash(str(path.resolve()), self.repo_name)

                    operation = {
                        "operation": "deleted",
                        "path": rel_path,
                        "relative_path": rel_path,
                        "absolute_path": str(path.resolve()),
                        "previous_hash": f"sha1:{previous_hash}" if previous_hash else None,
                        "file_hash": None,
                        "modified_time": datetime.now().isoformat(),
                        "language": CODE_EXTS.get(path.suffix.lower(), "unknown")
                    }
                    operations.append(operation)
                    # Once a delete operation has been recorded, drop the cache entry
                    # so subsequent scans do not keep re-reporting the same deletion.
                    remove_cached_file(str(path.resolve()), self.repo_name)

                except Exception as e:
                    print(f"[bundle_create] Error processing deleted file {path}: {e}")
                    continue

            # Create manifest
            manifest = {
                "version": "1.0",
                "bundle_id": bundle_id,
                "workspace_path": self.workspace_path,
                "collection_name": self.collection_name,
                "created_at": created_at,
                # CLI is stateless - server will assign sequence numbers
                "sequence_number": None,  # Server will assign
                "parent_sequence": None,   # Server will determine
                "operations": {
                    "created": len(changes["created"]),
                    "updated": len(changes["updated"]),
                    "deleted": len(changes["deleted"]),
                    "moved": len(changes["moved"])
                },
                "total_files": len(operations),
                "total_size_bytes": total_size,
                "compression": "gzip",
                "encoding": "utf-8"
            }

            # Write manifest
            (temp_path / "manifest.json").write_text(json.dumps(manifest, indent=2))

            # Write operations metadata
            operations_metadata = {
                "operations": operations
            }
            (metadata_dir / "operations.json").write_text(json.dumps(operations_metadata, indent=2))

            # Write hashes
            hashes_metadata = {
                "workspace_path": self.workspace_path,
                "updated_at": created_at,
                "file_hashes": file_hashes
            }
            (metadata_dir / "hashes.json").write_text(json.dumps(hashes_metadata, indent=2))

            # Create tarball in temporary directory
            temp_bundle_dir = self._get_temp_bundle_dir()
            bundle_path = temp_bundle_dir / f"{bundle_id}.tar.gz"
            with tarfile.open(bundle_path, "w:gz") as tar:
                tar.add(temp_path, arcname=f"{bundle_id}")

            return str(bundle_path), manifest

    def upload_bundle(self, bundle_path: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
        """
        Upload delta bundle to remote server with exponential backoff retry.

        Args:
            bundle_path: Path to the bundle tarball
            manifest: Bundle manifest metadata

        Returns:
            Server response dictionary
        """
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                # Simple exponential backoff
                if attempt > 0:
                    delay = min(2 ** (attempt - 1), 30)  # 1, 2, 4, 8... capped at 30s
                    logger.info(f"[remote_upload] Retry attempt {attempt + 1}/{self.max_retries + 1} after {delay}s delay")
                    time.sleep(delay)

                # Verify bundle exists
                if not os.path.exists(bundle_path):
                    return {"success": False, "error": {"code": "BUNDLE_NOT_FOUND", "message": f"Bundle not found: {bundle_path}"}}

                # Check bundle size (configurable client-side cap)
                max_bundle_mb = int(os.environ.get("REMOTE_UPLOAD_MAX_BUNDLE_MB", "100"))
                bundle_size = os.path.getsize(bundle_path)
                if bundle_size > max_bundle_mb * 1024 * 1024:
                    return {"success": False, "error": {"code": "BUNDLE_TOO_LARGE", "message": f"Bundle too large: {bundle_size} bytes"}}

                with open(bundle_path, 'rb') as bundle_file:
                    files = {
                        'bundle': (f"{manifest['bundle_id']}.tar.gz", bundle_file, 'application/gzip')
                    }

                    data = {
                        'workspace_path': self._translate_to_container_path(self.workspace_path),
                        'collection_name': self.collection_name,
                        # CLI is stateless - server handles sequence numbers
                        'force': 'false',
                        'source_path': self.workspace_path,
                    }

                    logger.info(f"[remote_upload] Uploading bundle {manifest['bundle_id']} (size: {bundle_size} bytes)")

                    response = self.session.post(
                        f"{self.upload_endpoint}/api/v1/delta/upload",
                        files=files,
                        data=data,
                        timeout=(10, self.timeout)
                    )

                    if response.status_code == 200:
                        result = response.json()
                        logger.info(f"[remote_upload] Successfully uploaded bundle {manifest['bundle_id']}")

                        seq = None
                        try:
                            seq = result.get("sequence_number")
                        except Exception:
                            seq = None
                        if seq is not None:
                            try:
                                manifest["sequence"] = seq
                            except Exception:
                                pass

                        poll_result = self._poll_after_timeout(manifest)
                        if poll_result.get("success"):
                            combined = dict(result)
                            for k, v in poll_result.items():
                                if k in ("success", "error"):
                                    continue
                                if k not in combined:
                                    combined[k] = v
                            return combined

                        logger.warning("[remote_upload] Upload accepted but polling did not confirm processing; returning original result")
                        return result
                    # Handle error
                    error_msg = f"Upload failed with status {response.status_code}"
                    try:
                        error_detail = response.json()
                        error_detail_msg = error_detail.get('error', {}).get('message', 'Unknown error')
                        error_msg += f": {error_detail_msg}"
                        error_code = error_detail.get('error', {}).get('code', 'HTTP_ERROR')
                    except:
                        error_msg += f": {response.text[:200]}"
                        error_code = "HTTP_ERROR"

                    last_error = {"success": False, "error": {"code": error_code, "message": error_msg, "status_code": response.status_code}}

                    # Don't retry on client errors (except 429)
                    if 400 <= response.status_code < 500 and response.status_code != 429:
                        return last_error

                    logger.warning(f"[remote_upload] Upload attempt {attempt + 1} failed: {error_msg}")

            except requests.exceptions.ConnectTimeout as e:
                last_error = {"success": False, "error": {"code": "TIMEOUT_ERROR", "message": f"Upload timeout: {str(e)}"}}
                logger.warning(f"[remote_upload] Upload timeout on attempt {attempt + 1}: {e}")

            except requests.exceptions.ReadTimeout as e:
                last_error = {"success": False, "error": {"code": "TIMEOUT_ERROR", "message": f"Upload timeout: {str(e)}"}}
                logger.warning(f"[remote_upload] Upload read timeout on attempt {attempt + 1}: {e}")
                
                # After read timeout, poll to check if server processed the bundle
                logger.info(f"[remote_upload] Read timeout occurred, polling server to check if bundle was processed...")
                poll_result = self._poll_after_timeout(manifest)
                if poll_result.get("success"):
                    logger.info(f"[remote_upload] Server confirmed processing of bundle {manifest['bundle_id']} after timeout")
                    return poll_result
                
                logger.warning(f"[remote_upload] Server did not process bundle after timeout, proceeding with failure")
                break

            except requests.exceptions.Timeout as e:
                last_error = {"success": False, "error": {"code": "TIMEOUT_ERROR", "message": f"Upload timeout: {str(e)}"}}
                logger.warning(f"[remote_upload] Upload timeout on attempt {attempt + 1}: {e}")
                
                # For generic timeout, also try polling
                logger.info(f"[remote_upload] Timeout occurred, polling server to check if bundle was processed...")
                poll_result = self._poll_after_timeout(manifest)
                if poll_result.get("success"):
                    logger.info(f"[remote_upload] Server confirmed processing of bundle {manifest['bundle_id']} after timeout")
                    return poll_result
                
                logger.warning(f"[remote_upload] Server did not process bundle after timeout, proceeding with failure")
                break

            except requests.exceptions.ConnectionError as e:
                last_error = {"success": False, "error": {"code": "CONNECTION_ERROR", "message": f"Connection error: {str(e)}"}}
                logger.warning(f"[remote_upload] Connection error on attempt {attempt + 1}: {e}")

            except requests.exceptions.RequestException as e:
                last_error = {"success": False, "error": {"code": "NETWORK_ERROR", "message": f"Network error: {str(e)}"}}
                logger.warning(f"[remote_upload] Network error on attempt {attempt + 1}: {e}")

            except Exception as e:
                last_error = {"success": False, "error": {"code": "UPLOAD_ERROR", "message": f"Upload error: {str(e)}"}}
                logger.error(f"[remote_upload] Unexpected error on attempt {attempt + 1}: {e}")

        # All retries exhausted
        logger.error(f"[remote_upload] All {self.max_retries + 1} upload attempts failed for bundle {manifest.get('bundle_id', 'unknown')}")
        return last_error or {
            "success": False,
            "error": {
                "code": "MAX_RETRIES_EXCEEDED",
                "message": f"Upload failed after {self.max_retries + 1} attempts"
            }
        }

    def _poll_after_timeout(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        """
        Poll server status after a timeout to check if bundle was processed.
        
        Args:
            manifest: Bundle manifest containing sequence information
            
        Returns:
            Dictionary indicating success if bundle was processed
        """
        try:
            # Get current server status to know the expected sequence
            status = self.get_server_status()
            if not status.get("success"):
                return {"success": False, "error": status.get("error", {"code": "UNKNOWN", "message": "Failed to get status"})}

            current_sequence = status.get("last_sequence", 0)
            expected_sequence = manifest.get("sequence", current_sequence + 1)

            logger.info(f"[remote_upload] Current server sequence: {current_sequence}, expected: {expected_sequence}")

            # If server is already at expected sequence, bundle was processed
            if current_sequence >= expected_sequence:
                return {
                    "success": True,
                    "message": f"Bundle processed (server at sequence {current_sequence})",
                    "sequence": current_sequence,
                }

            # Poll window is configurable via REMOTE_UPLOAD_POLL_MAX_SECS (seconds).
            # Values <= 0 mean "no timeout" (poll until success or process exit).
            try:
                max_poll_time = int(os.environ.get("REMOTE_UPLOAD_POLL_MAX_SECS", "300"))
            except Exception:
                max_poll_time = 300
            poll_interval = 5
            start_time = time.time()

            while True:
                elapsed = time.time() - start_time
                if max_poll_time > 0 and elapsed >= max_poll_time:
                    logger.warning(
                        f"[remote_upload] Polling timed out after {int(elapsed)}s (limit={max_poll_time}s), bundle was not confirmed as processed"
                    )
                    return {
                        "success": False,
                        "error": {
                            "code": "POLL_TIMEOUT",
                            "message": f"Bundle not confirmed processed after polling for {int(elapsed)}s (limit={max_poll_time}s)",
                        },
                    }

                logger.info(
                    f"[remote_upload] Polling server status... (elapsed: {int(elapsed)}s, limit={'no-limit' if max_poll_time <= 0 else max_poll_time}s)"
                )
                time.sleep(poll_interval)

                status = self.get_server_status()
                if status.get("success"):
                    new_sequence = status.get("last_sequence", 0)
                    if new_sequence >= expected_sequence:
                        logger.info(
                            f"[remote_upload] Server sequence advanced to {new_sequence}, bundle was processed!"
                        )
                        return {
                            "success": True,
                            "message": f"Bundle processed after timeout (server at sequence {new_sequence})",
                            "sequence": new_sequence,
                        }
                    logger.debug(
                        f"[remote_upload] Server sequence still at {new_sequence}, continuing to poll..."
                    )
                else:
                    logger.warning(
                        f"[remote_upload] Failed to get server status during poll: {status.get('error', {}).get('message', 'Unknown')}"
                    )

        except Exception as e:
            logger.error(f"[remote_upload] Error during post-timeout polling: {e}")
            return {"success": False, "error": {"code": "POLL_ERROR", "message": f"Polling error: {str(e)}"}}

    def get_server_status(self) -> Dict[str, Any]:
        """Get server status with simplified error handling."""
        try:
            container_workspace_path = self._translate_to_container_path(self.workspace_path)
            connect_timeout = min(self.timeout, 10)
            # Allow slower responses (e.g., cold starts/large collections) before bailing
            read_timeout = max(self.timeout, 30)
            response = self.session.get(
                f"{self.upload_endpoint}/api/v1/delta/status",
                params={'workspace_path': container_workspace_path},
                timeout=(connect_timeout, read_timeout)
            )

            if response.status_code == 200:
                return response.json()

            # Handle error response
            error_msg = f"Status check failed with HTTP {response.status_code}"
            try:
                error_detail = response.json()
                error_msg += f": {error_detail.get('error', {}).get('message', 'Unknown error')}"
            except:
                error_msg += f": {response.text[:100]}"

            return {"success": False, "error": {"code": "STATUS_ERROR", "message": error_msg}}

        except requests.exceptions.Timeout:
            return {"success": False, "error": {"code": "STATUS_TIMEOUT", "message": "Status check timeout"}}
        except requests.exceptions.ConnectionError:
            return {"success": False, "error": {"code": "CONNECTION_ERROR", "message": f"Cannot connect to server"}}
        except Exception as e:
            return {"success": False, "error": {"code": "STATUS_CHECK_ERROR", "message": f"Status check error: {str(e)}"}}

    def has_meaningful_changes(self, changes: Dict[str, List]) -> bool:
        """Check if changes warrant a delta upload."""
        total_changes = sum(len(files) for op, files in changes.items() if op != "unchanged")
        return total_changes > 0

    def process_changes_and_upload(self, changes: Dict[str, List]) -> bool:
        """
        Process pre-computed changes and upload delta bundle.
        Includes comprehensive error handling and graceful fallback.

        Args:
            changes: Dictionary of file changes by type

        Returns:
            True if upload was successful, False otherwise
        """
        try:
            logger.info(f"[remote_upload] Processing pre-computed changes")

            # Validate input
            if not changes:
                logger.info("[remote_upload] No changes provided")
                return True

            if not self.has_meaningful_changes(changes):
                logger.info("[remote_upload] No meaningful changes detected, skipping upload")
                return True

            # Log change summary
            total_changes = sum(len(files) for op, files in changes.items() if op != "unchanged")
            logger.info(f"[remote_upload] Detected {total_changes} meaningful changes: "
                       f"{len(changes['created'])} created, {len(changes['updated'])} updated, "
                       f"{len(changes['deleted'])} deleted, {len(changes['moved'])} moved")

            # Create delta bundle
            bundle_path = None
            try:
                bundle_path, manifest = self.create_delta_bundle(changes)
                logger.info(f"[remote_upload] Created delta bundle: {manifest['bundle_id']} "
                           f"(size: {manifest['total_size_bytes']} bytes)")

                # Validate bundle was created successfully
                if not bundle_path or not os.path.exists(bundle_path):
                    raise RuntimeError(f"Failed to create bundle at {bundle_path}")

            except Exception as e:
                logger.error(f"[remote_upload] Error creating delta bundle: {e}")
                # Clean up any temporary files on failure
                self.cleanup()
                return False

            # Upload bundle with retry logic
            try:
                response = self.upload_bundle(bundle_path, manifest)

                if response.get("success", False):
                    processed_ops = response.get('processed_operations', {})
                    logger.info(f"[remote_upload] Successfully uploaded bundle {manifest['bundle_id']}")
                    logger.info(f"[remote_upload] Processed operations: {processed_ops}")

                    # Clean up temporary bundle after successful upload
                    try:
                        if os.path.exists(bundle_path):
                            os.remove(bundle_path)
                            logger.debug(f"[remote_upload] Cleaned up temporary bundle: {bundle_path}")
                        # Also clean up the entire temp directory if this is the last bundle
                        self.cleanup()
                    except Exception as cleanup_error:
                        logger.warning(f"[remote_upload] Failed to cleanup bundle {bundle_path}: {cleanup_error}")

                    return True
                else:
                    error_msg = response.get('error', {}).get('message', 'Unknown upload error')
                    logger.error(f"[remote_upload] Upload failed: {error_msg}")
                    return False

            except Exception as e:
                logger.error(f"[remote_upload] Error uploading bundle: {e}")
                return False

        except Exception as e:
            logger.error(f"[remote_upload] Unexpected error in process_changes_and_upload: {e}")
            return False

    def watch_loop(self, interval: int = 5):
        """Main file watching loop using existing detection and upload methods."""
        logger.info(f"[watch] Starting file monitoring (interval: {interval}s)")
        logger.info(f"[watch] Monitoring: {self.workspace_path}")
        logger.info(f"[watch] Press Ctrl+C to stop")

        try:
            while True:
                try:
                    # Use existing change detection over both filesystem and cached registry
                    fs_files = self.get_all_code_files()
                    path_map = {}
                    for p in fs_files:
                        try:
                            resolved = p.resolve()
                        except Exception:
                            continue
                        path_map[resolved] = p

                    # Include any paths that are only present in the local cache (deleted files)
                    for cached_abs in get_all_cached_paths(self.repo_name):
                        try:
                            cached_path = Path(cached_abs)
                            resolved = cached_path.resolve()
                        except Exception:
                            continue
                        if resolved not in path_map:
                            path_map[resolved] = cached_path

                    all_paths = list(path_map.values())
                    changes = self.detect_file_changes(all_paths)

                    # Count only meaningful changes (exclude unchanged)
                    meaningful_changes = len(changes.get("created", [])) + len(changes.get("updated", [])) + len(changes.get("deleted", [])) + len(changes.get("moved", []))

                    if meaningful_changes > 0:
                        logger.info(f"[watch] Detected {meaningful_changes} changes: { {k: len(v) for k, v in changes.items() if k != 'unchanged'} }")

                        # Use existing upload method
                        success = self.process_changes_and_upload(changes)

                        if success:
                            logger.info(f"[watch] Successfully uploaded changes")
                        else:
                            logger.error(f"[watch] Failed to upload changes")
                    else:
                        logger.debug(f"[watch] No changes detected")  # Debug level to avoid spam

                    # Sleep until next check
                    time.sleep(interval)

                except KeyboardInterrupt:
                    logger.info(f"[watch] Received interrupt signal, stopping...")
                    break
                except Exception as e:
                    logger.error(f"[watch] Error in watch loop: {e}")
                    time.sleep(interval)  # Continue even after errors

        except KeyboardInterrupt:
            logger.info(f"[watch] File monitoring stopped by user")

    def get_all_code_files(self) -> List[Path]:
        """Get all code files in the workspace, excluding heavy/third-party dirs."""
        files: List[Path] = []
        try:
            workspace_path = Path(self.workspace_path)
            if not workspace_path.exists():
                return files

            # Single walk with early pruning and set-based matching to reduce IO
            ext_suffixes = {str(ext).lower() for ext in CODE_EXTS if str(ext).startswith('.')}
            name_matches = {str(ext) for ext in CODE_EXTS if not str(ext).startswith('.')}
            dev_remote = os.environ.get("DEV_REMOTE_MODE") == "1" or os.environ.get("REMOTE_UPLOAD_MODE") == "development"
            excluded = {
                "node_modules", "vendor", "dist", "build", "target", "out",
                ".git", ".hg", ".svn", ".vscode", ".idea", ".venv", "venv",
                "__pycache__", ".pytest_cache", ".mypy_cache", ".cache",
                ".context-engine", ".context-engine-uploader", ".codebase"
            }
            if dev_remote:
                excluded.add("dev-workspace")

            seen = set()
            for root, dirnames, filenames in os.walk(workspace_path):
                # Prune heavy/hidden directories before descending
                dirnames[:] = [d for d in dirnames if d not in excluded and not d.startswith('.')]

                for filename in filenames:
                    if filename.startswith('.'):
                        continue
                    candidate = Path(root) / filename
                    suffix = candidate.suffix.lower()
                    if filename in name_matches or suffix in ext_suffixes:
                        resolved = candidate.resolve()
                        if resolved not in seen:
                            seen.add(resolved)
                            files.append(candidate)
        except Exception as e:
            logger.error(f"[watch] Error scanning files: {e}")

        return files

    def process_and_upload_changes(self, changed_paths: List[Path]) -> bool:
        """
        Process changed paths and upload delta bundle if meaningful changes exist.
        Includes comprehensive error handling and graceful fallback.

        Args:
            changed_paths: List of changed file paths

        Returns:
            True if upload was successful, False otherwise
        """
        try:
            logger.info(f"[remote_upload] Processing {len(changed_paths)} changed paths")

            # Validate input
            if not changed_paths:
                logger.info("[remote_upload] No changed paths provided")
                return True

            # Detect changes
            try:
                changes = self.detect_file_changes(changed_paths)
            except Exception as e:
                logger.error(f"[remote_upload] Error detecting file changes: {e}")
                return False

            if not self.has_meaningful_changes(changes):
                logger.info("[remote_upload] No meaningful changes detected, skipping upload")
                return True

            # Log change summary
            total_changes = sum(len(files) for op, files in changes.items() if op != "unchanged")
            logger.info(f"[remote_upload] Detected {total_changes} meaningful changes: "
                       f"{len(changes['created'])} created, {len(changes['updated'])} updated, "
                       f"{len(changes['deleted'])} deleted, {len(changes['moved'])} moved")

            # Create delta bundle
            bundle_path = None
            try:
                bundle_path, manifest = self.create_delta_bundle(changes)
                logger.info(f"[remote_upload] Created delta bundle: {manifest['bundle_id']} "
                           f"(size: {manifest['total_size_bytes']} bytes)")

                # Validate bundle was created successfully
                if not bundle_path or not os.path.exists(bundle_path):
                    raise RuntimeError(f"Failed to create bundle at {bundle_path}")

            except Exception as e:
                logger.error(f"[remote_upload] Error creating delta bundle: {e}")
                # Clean up any temporary files on failure
                self.cleanup()
                return False

            # Upload bundle with retry logic
            try:
                response = self.upload_bundle(bundle_path, manifest)

                if response.get("success", False):
                    processed_ops = response.get('processed_operations', {})
                    logger.info(f"[remote_upload] Successfully uploaded bundle {manifest['bundle_id']}")
                    logger.info(f"[remote_upload] Processed operations: {processed_ops}")

                    # Clean up temporary bundle after successful upload
                    try:
                        if os.path.exists(bundle_path):
                            os.remove(bundle_path)
                            logger.debug(f"[remote_upload] Cleaned up temporary bundle: {bundle_path}")
                        # Also clean up the entire temp directory if this is the last bundle
                        self.cleanup()
                    except Exception as cleanup_error:
                        logger.warning(f"[remote_upload] Failed to cleanup bundle {bundle_path}: {cleanup_error}")

                    return True
                else:
                    error = response.get("error", {})
                    error_code = error.get("code", "UNKNOWN")
                    error_msg = error.get("message", "Unknown error")

                    logger.error(f"[remote_upload] Upload failed: {error_msg}")

                    # Handle specific error types
                    # CLI is stateless - server handles sequence management
                    if error_code in ["BUNDLE_TOO_LARGE", "BUNDLE_NOT_FOUND"]:
                        # These are unrecoverable errors
                        logger.error(f"[remote_upload] Unrecoverable error ({error_code}): {error_msg}")
                        return False
                    elif error_code in ["TIMEOUT_ERROR", "CONNECTION_ERROR", "NETWORK_ERROR"]:
                        # These might be temporary, suggest fallback
                        logger.warning(f"[remote_upload] Network-related error ({error_code}): {error_msg}")
                        logger.warning("[remote_upload] Consider falling back to local mode if this persists")
                        return False
                    else:
                        # Other errors
                        logger.error(f"[remote_upload] Upload error ({error_code}): {error_msg}")
                        return False

            except Exception as e:
                logger.error(f"[remote_upload] Unexpected error during upload: {e}")
                return False

        except Exception as e:
            logger.error(f"[remote_upload] Critical error in process_and_upload_changes: {e}")
            logger.exception("[remote_upload] Full traceback:")
            return False

def get_remote_config(cli_path: Optional[str] = None) -> Dict[str, str]:
    """Get remote upload configuration from environment variables and command-line arguments."""
    # Use command-line path if provided, otherwise fall back to environment variables
    if cli_path:
        workspace_path = cli_path
    else:
        workspace_path = os.environ.get("WATCH_ROOT", os.environ.get("WORKSPACE_PATH", "/work"))

    # Use auto-generated collection name based on repo name
    repo_name = _extract_repo_name_from_path(workspace_path)
    # Fallback to directory name if repo detection fails
    if not repo_name:
        repo_name = Path(workspace_path).name
    collection_name = get_collection_name(repo_name)

    return {
        "upload_endpoint": os.environ.get("REMOTE_UPLOAD_ENDPOINT", "http://localhost:8080"),
        "workspace_path": workspace_path,
        "collection_name": collection_name,
        # Use higher, more robust defaults but still allow env overrides
        "max_retries": int(os.environ.get("REMOTE_UPLOAD_MAX_RETRIES", "5")),
        "timeout": int(os.environ.get("REMOTE_UPLOAD_TIMEOUT", "1800")),
    }


def main():
    """Main entry point for the remote upload client."""
    parser = argparse.ArgumentParser(
        description="Remote upload client for delta bundles in Context-Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Upload from current directory or environment variables
  python remote_upload_client.py

  # Upload from specific directory
  python remote_upload_client.py --path /path/to/repo

  # Upload from specific directory with custom endpoint
  python remote_upload_client.py --path /path/to/repo --endpoint http://remote-server:8080
        """
    )

    parser.add_argument(
        "--path",
        type=str,
        help="Path to the directory to upload (overrides WATCH_ROOT/WORKSPACE_PATH environment variables)"
    )

    parser.add_argument(
        "--endpoint",
        type=str,
        help="Remote upload endpoint (overrides REMOTE_UPLOAD_ENDPOINT environment variable)"
    )

    parser.add_argument(
        "--max-retries",
        type=int,
        help="Maximum number of upload retries (overrides REMOTE_UPLOAD_MAX_RETRIES environment variable)"
    )

    parser.add_argument(
        "--timeout",
        type=int,
        help="Request timeout in seconds (overrides REMOTE_UPLOAD_TIMEOUT environment variable)"
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Force upload of all files (ignore cached state and treat all files as new)"
    )

    parser.add_argument(
        "--show-mapping",
        action="store_true",
        help="Print collection↔workspace mapping information and exit"
    )

    parser.add_argument(
        "--watch", "-w",
        action="store_true",
        help="Watch for file changes and upload automatically (continuous mode)"
    )

    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=5,
        help="Watch interval in seconds (default: 5)"
    )

    args = parser.parse_args()

    # Validate path if provided
    if args.path:
        if not os.path.exists(args.path):
            logger.error(f"Path does not exist: {args.path}")
            return 1

        if not os.path.isdir(args.path):
            logger.error(f"Path is not a directory: {args.path}")
            return 1

        args.path = os.path.abspath(args.path)
        logger.info(f"Using specified path: {args.path}")

    # Get configuration
    config = get_remote_config(args.path)

    # Override config with command-line arguments if provided
    if args.endpoint:
        config["upload_endpoint"] = args.endpoint
    if args.max_retries is not None:
        config["max_retries"] = args.max_retries
    if args.timeout is not None:
        config["timeout"] = args.timeout

    logger.info(f"Workspace path: {config['workspace_path']}")
    logger.info(f"Collection name: {config['collection_name']}")
    logger.info(f"Upload endpoint: {config['upload_endpoint']}")

    if args.show_mapping:
        with RemoteUploadClient(
            upload_endpoint=config["upload_endpoint"],
            workspace_path=config["workspace_path"],
            collection_name=config["collection_name"],
            max_retries=config["max_retries"],
            timeout=config["timeout"],
        ) as client:
            client.log_mapping_summary()
        return 0

    # Handle watch mode
    if args.watch:
        logger.info("Starting watch mode for continuous file monitoring")
        try:
            with RemoteUploadClient(
                upload_endpoint=config["upload_endpoint"],
                workspace_path=config["workspace_path"],
                collection_name=config["collection_name"],
                max_retries=config["max_retries"],
                timeout=config["timeout"]
            ) as client:

                logger.info("Remote upload client initialized successfully")
                client.log_mapping_summary()

                # Test server connection first
                logger.info("Checking server status...")
                status = client.get_server_status()
                is_success = (
                    isinstance(status, dict) and
                    'workspace_path' in status and
                    'collection_name' in status and
                    status.get('status') == 'ready'
                )
                if not is_success:
                    error = status.get("error", {})
                    logger.error(f"Cannot connect to server: {error.get('message', 'Unknown error')}")
                    return 1

                logger.info("Server connection successful")
                logger.info(f"Starting file monitoring with {args.interval}s interval")

                # Start the watch loop
                client.watch_loop(interval=args.interval)

            return 0

        except KeyboardInterrupt:
            logger.info("Watch mode stopped by user")
            return 0
        except Exception as e:
            logger.error(f"Watch mode failed: {e}")
            return 1

    # Single upload mode (original logic)
    # Initialize client with context manager for cleanup
    try:
        with RemoteUploadClient(
            upload_endpoint=config["upload_endpoint"],
            workspace_path=config["workspace_path"],
            collection_name=config["collection_name"],
            max_retries=config["max_retries"],
            timeout=config["timeout"]
        ) as client:

            logger.info("Remote upload client initialized successfully")

            client.log_mapping_summary()

            # Test server connection
            logger.info("Checking server status...")
            status = client.get_server_status()
            # For delta endpoint, success is indicated by having expected fields (not a "success" boolean)
            is_success = (
                isinstance(status, dict) and
                'workspace_path' in status and
                'collection_name' in status and
                status.get('status') == 'ready'
            )
            if not is_success:
                error = status.get("error", {})
                logger.error(f"Cannot connect to server: {error.get('message', 'Unknown error')}")
                return 1

            logger.info("Server connection successful")

            # Scan repository and upload files
            logger.info("Scanning repository for files...")
            workspace_path = Path(config['workspace_path'])

            # Find code files in the repository (exclude hidden and heavy dirs)
            all_files = client.get_all_code_files()
            logger.info(f"Found {len(all_files)} code files to upload")

            if not all_files:
                logger.warning("No files found to upload")
                return 0

            # Detect changes (treat all files as changes for initial upload)
            if args.force:
                # Force mode: treat all files as created
                changes = {"created": all_files, "updated": [], "deleted": [], "moved": [], "unchanged": []}
            else:
                changes = client.detect_file_changes(all_files)

            if not client.has_meaningful_changes(changes):
                logger.info("No meaningful changes to upload")
                return 0

            logger.info(f"Changes detected: {len(changes.get('created', []))} created, {len(changes.get('updated', []))} updated, {len(changes.get('deleted', []))} deleted")

            # Process and upload changes
            logger.info("Uploading files to remote server...")
            success = client.process_changes_and_upload(changes)

            if success:
                logger.info("Repository upload completed successfully!")
                logger.info(f"Collection name: {config['collection_name']}")
                logger.info(f"Files uploaded: {len(all_files)}")
            else:
                logger.error("Repository upload failed!")
                return 1

            return 0

    except Exception as e:
        logger.error(f"Failed to initialize remote upload client: {e}")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
