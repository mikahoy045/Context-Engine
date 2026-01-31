#!/usr/bin/env python3
"""
ingest/pipeline.py - Core indexing pipeline.

This module provides the main indexing functions: index_single_file, index_repo,
process_file_with_smart_reindexing, and related orchestration logic.
"""
from __future__ import annotations

import logging
import os
import sys
import time

import xxhash
import multiprocessing
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import List, Dict, Any, Optional, TYPE_CHECKING

logger = logging.getLogger(__name__)

# Import lock contention handling for skip-and-retry logic during parallel indexing
try:
    from scripts.workspace_state import LockContendedException, enable_lock_skip_on_contention
except ImportError:
    # Fallback if not available
    class LockContendedException(Exception):
        pass

    @contextmanager
    def enable_lock_skip_on_contention():
        yield

from qdrant_client import QdrantClient, models

from scripts.ingest.config import (
    ROOT_DIR,
    CODE_EXTS,
    EXTENSIONLESS_FILES,
    LEX_VECTOR_NAME,
    LEX_VECTOR_DIM,
    LEX_SPARSE_NAME,
    LEX_SPARSE_MODE,
    MINI_VECTOR_NAME,
    MINI_VEC_DIM,
    # Multi-granular vectors
    MULTI_GRANULAR_VECTORS,
    ENTITY_DENSE_NAME,
    ENTITY_DENSE_DIM,
    RELATION_DENSE_NAME,
    RELATION_DENSE_DIM,
    _env_truthy,
    is_multi_repo_mode,
    get_collection_name,
    logical_repo_reuse_enabled,
    get_cached_file_hash,
    set_cached_file_hash,
    get_cached_symbols,
    set_cached_symbols,
    get_cached_pseudo,
    set_cached_pseudo,
    get_cached_file_meta,
    get_workspace_state,
    compare_symbol_changes,
    file_indexing_lock,
    set_indexing_started,
    set_indexing_progress,
)
from scripts.ingest.exclusions import (
    iter_files,
    _should_skip_explicit_file_by_excluder,
)
from scripts.ingest.chunking import chunk_lines, chunk_semantic, chunk_by_tokens, chunk_semantic_v2, chunk_search_optimized_v1, chunk_cast_plus_v1
from scripts.ingest.symbols import (
    _extract_symbols,
    _choose_symbol_for_chunk,
    extract_symbols_with_tree_sitter,
    _Sym,
)
from scripts.ingest.pseudo import (
    generate_pseudo_tags,
    should_process_pseudo_for_chunk,
)
from scripts.ingest.metadata import (
    _git_metadata,
    _get_imports_calls,
    _get_inheritance,
    _compute_host_and_container_paths,
)
from scripts.ingest.vectors import project_mini, extract_pattern_vector
from scripts.ingest.qdrant import (
    ensure_collection,
    ensure_collection_and_indexes_once,
    ensure_payload_indexes,
    recreate_collection,
    get_collection_vector_names,
    get_indexed_file_hash,
    delete_points_by_path,
    upsert_points,
    hash_id,
    embed_batch,
    PATTERN_VECTOR_NAME,
)
# Graph edges - route through backend adapter when Neo4j is enabled
from scripts.graph_backends import is_neo4j_enabled

_NEO4J_GRAPH_ENABLED = is_neo4j_enabled()

if _NEO4J_GRAPH_ENABLED:
    # Use backend abstraction layer for Neo4j support
    from scripts.graph_backends.ingest_adapter import (
        ensure_graph_store as ensure_graph_collection,
        extract_call_edges as _extract_call_edges_adapter,
        extract_import_edges as _extract_import_edges_adapter,
        extract_inheritance_edges as _extract_inheritance_edges_adapter,
        upsert_edges as _upsert_edges_adapter,
        delete_edges_by_path,
        GRAPH_COLLECTION_SUFFIX,
    )

    def get_graph_collection_name(base: str) -> str:
        return f"{base}{GRAPH_COLLECTION_SUFFIX}"

    # Adapters convert GraphEdge objects to dicts for existing code
    def extract_call_edges(**kwargs):
        edges = _extract_call_edges_adapter(**kwargs)
        return [{"id": e.id, "payload": e.to_dict()} for e in edges]

    def extract_import_edges(**kwargs):
        edges = _extract_import_edges_adapter(**kwargs)
        return [{"id": e.id, "payload": e.to_dict()} for e in edges]

    def extract_inheritance_edges(**kwargs):
        edges = _extract_inheritance_edges_adapter(**kwargs)
        return [{"id": e.id, "payload": e.to_dict()} for e in edges]

    def upsert_edges(client, graph_coll, edges, batch_size=100):
        # Convert dict edges back to GraphEdge objects
        from scripts.graph_backends.base import GraphEdge
        graph_edges = []
        for e in edges:
            payload = e.get("payload", {})
            graph_edges.append(GraphEdge(
                id=e.get("id", ""),
                caller_symbol=payload.get("caller_symbol", ""),
                callee_symbol=payload.get("callee_symbol", ""),
                caller_path=payload.get("caller_path", ""),
                callee_path=payload.get("callee_path"),  # Resolved callee path
                edge_type=payload.get("edge_type", ""),
                repo=payload.get("repo", ""),
                start_line=payload.get("start_line"),
                end_line=payload.get("end_line"),
                language=payload.get("language"),
                caller_point_id=payload.get("caller_point_id"),
            ))
        return _upsert_edges_adapter(client, graph_coll, graph_edges, batch_size)
else:
    # Default: use existing Qdrant-native graph_edges
    from scripts.ingest.graph_edges import (
        ensure_graph_collection,
        extract_call_edges,
        extract_import_edges,
        extract_inheritance_edges,
        upsert_edges,
        delete_edges_by_path,
        get_graph_collection_name,
    )

# Import utility functions
from scripts.utils import sanitize_vector_name as _sanitize_vector_name
from scripts.utils import lex_hash_vector_text as _lex_hash_vector_text
from scripts.utils import lex_sparse_vector_text as _lex_sparse_vector_text

if TYPE_CHECKING:
    from fastembed import TextEmbedding


# -----------------------------------------------------------------------------
# Edge extraction wrappers - handle Neo4j vs Qdrant signature differences
# Neo4j adapter accepts extra params (import_paths, collection, qdrant_client)
# Qdrant-native implementation does not accept these params
# -----------------------------------------------------------------------------

def _extract_call_edges_compat(
    symbol_path: str,
    calls: list,
    path: str,
    repo: str,
    start_line: int = None,
    end_line: int = None,
    language: str = None,
    caller_point_id: str = None,
    # Neo4j-only params (ignored when Neo4j disabled)
    import_paths: dict = None,
    collection: str = None,
    qdrant_client=None,
) -> list:
    """Wrapper for extract_call_edges that handles Neo4j vs Qdrant signatures."""
    if _NEO4J_GRAPH_ENABLED:
        return extract_call_edges(
            symbol_path=symbol_path,
            calls=calls,
            path=path,
            repo=repo,
            start_line=start_line,
            end_line=end_line,
            language=language,
            caller_point_id=caller_point_id,
            import_paths=import_paths,
            collection=collection,
            qdrant_client=qdrant_client,
        )
    else:
        return extract_call_edges(
            symbol_path=symbol_path,
            calls=calls,
            path=path,
            repo=repo,
            start_line=start_line,
            end_line=end_line,
            language=language,
            caller_point_id=caller_point_id,
        )


def _extract_import_edges_compat(
    symbol_path: str,
    imports: list,
    path: str,
    repo: str,
    language: str = None,
    caller_point_id: str = None,
    # Neo4j-only params (ignored when Neo4j disabled)
    collection: str = None,
    qdrant_client=None,
) -> list:
    """Wrapper for extract_import_edges that handles Neo4j vs Qdrant signatures."""
    if _NEO4J_GRAPH_ENABLED:
        return extract_import_edges(
            symbol_path=symbol_path,
            imports=imports,
            path=path,
            repo=repo,
            language=language,
            caller_point_id=caller_point_id,
            collection=collection,
            qdrant_client=qdrant_client,
        )
    else:
        return extract_import_edges(
            symbol_path=symbol_path,
            imports=imports,
            path=path,
            repo=repo,
            language=language,
            caller_point_id=caller_point_id,
        )

try:
    from scripts.ast_analyzer import get_ast_analyzer
    _AST_ANALYZER_AVAILABLE = True
except ImportError:
    _AST_ANALYZER_AVAILABLE = False


def _detect_repo_name_from_path(path: Path) -> str:
    """Wrapper function to use workspace_state repository detection."""
    try:
        from scripts.workspace_state import _extract_repo_name_from_path as _ws_detect
        return _ws_detect(str(path))
    except ImportError:
        return path.name if path.is_dir() else path.parent.name


def detect_language(path: Path) -> str:
    """Detect language from file extension or name pattern."""
    ext_lang = CODE_EXTS.get(path.suffix.lower())
    if ext_lang:
        return ext_lang
    fname_lower = path.name.lower()
    if fname_lower in EXTENSIONLESS_FILES:
        return EXTENSIONLESS_FILES[fname_lower]
    if fname_lower.startswith("dockerfile"):
        return "dockerfile"
    return "unknown"


_TEXT_LIKE_LANGS = {"unknown", "markdown", "text"}


def _is_text_like_language(language: str) -> bool:
    return str(language or "").strip().lower() in _TEXT_LIKE_LANGS


def _dedupe_preserve(items: List[str]) -> List[str]:
    """Deduplicate while preserving order."""
    out: List[str] = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        out.append(item)
        seen.add(item)
    return out


def _slice_calls_for_range(calls: List[Any], start: int, end: int) -> List[str]:
    """Extract call names whose line is within the chunk range."""
    matched: List[str] = []
    for call in calls:
        try:
            line = int(getattr(call, "line", 0) or 0)
        except Exception:
            line = 0
        if line < start or line > end:
            continue
        callee = str(getattr(call, "callee", "") or "")
        if callee:
            matched.append(callee)
    return _dedupe_preserve(matched)


def _slice_imports_for_range(imports: List[Any], start: int, end: int) -> List[str]:
    """Extract import modules whose line is within the chunk range."""
    matched: List[str] = []
    for imp in imports:
        try:
            line = int(getattr(imp, "line", 0) or 0)
        except Exception:
            line = 0
        if line < start or line > end:
            continue
        module = str(getattr(imp, "module", "") or "")
        if module:
            matched.append(module)
    return _dedupe_preserve(matched)


def _ast_analyze_file(file_path: Path, language: str, text: str) -> Dict[str, Any]:
    """Run AST analyzer for richer symbol/call/import metadata when available."""
    if not _AST_ANALYZER_AVAILABLE:
        return {}
    try:
        analyzer = get_ast_analyzer()
        analysis = analyzer.analyze_file(str(file_path), language, text)
    except Exception:
        return {}

    symbols = analysis.get("symbols", []) or []
    imports = analysis.get("imports", []) or []
    calls = analysis.get("calls", []) or []

    symbol_spans: List[Dict[str, Any]] = []
    symbol_meta_by_path: Dict[str, Any] = {}
    symbol_meta_by_name: Dict[str, Any] = {}
    for sym in symbols:
        name = str(getattr(sym, "name", "") or "")
        kind = str(getattr(sym, "kind", "") or "")
        start = int(getattr(sym, "start_line", 0) or 0)
        end = int(getattr(sym, "end_line", 0) or 0)
        path = str(getattr(sym, "path", "") or "") or name
        if not name or not start or not end:
            continue
        symbol_spans.append(
            _Sym(name=name, kind=kind, start=start, end=end, path=path)
        )
        symbol_meta_by_path[path] = sym
        symbol_meta_by_name.setdefault(name, sym)

    import_modules = _dedupe_preserve(
        [str(getattr(imp, "module", "") or "") for imp in imports if getattr(imp, "module", None)]
    )
    call_names = _dedupe_preserve(
        [str(getattr(call, "callee", "") or "") for call in calls if getattr(call, "callee", None)]
    )

    symbol_calls: Dict[str, List[str]] = {}
    for call in calls:
        caller = str(getattr(call, "caller", "") or "")
        callee = str(getattr(call, "callee", "") or "")
        if not caller or not callee:
            continue
        symbol_calls.setdefault(caller, [])
        symbol_calls[caller].append(callee)
    for caller, callees in list(symbol_calls.items()):
        symbol_calls[caller] = _dedupe_preserve(callees)

    return {
        "symbol_spans": symbol_spans,
        "symbol_meta_by_path": symbol_meta_by_path,
        "symbol_meta_by_name": symbol_meta_by_name,
        "imports": import_modules,
        "calls": call_names,
        "import_refs": imports,
        "call_refs": calls,
        "symbol_calls": symbol_calls,
    }


def _select_dense_text(
    *,
    info: str,
    code_text: str,
    pseudo: str = "",
    tags: list[str] | None = None,
    mode: str | None = None,
) -> str:
    """Choose the text used for dense embedding.

    Default is info+pseudo+tags to emphasize intent-rich context:
    - info = "{language} code from {path} lines {start}-{end}. {first_line}"
    - pseudo/tags = semantic enrichment from LLM
    Dense captures the "what" (intent), lexical handles the "how" (code body).
    """
    if mode is None:
        env_mode = str(os.environ.get("INDEX_DENSE_MODE", "") or "").strip().lower()
        mode = env_mode or "info+pseudo+tags"
    else:
        mode = str(mode).strip().lower()
    # Default dense cap depends on embedding model context window.
    # bge-m3 supports ~8k tokens, so we allow a larger character budget to preserve code context.
    max_chars_env = os.environ.get("INDEX_DENSE_MAX_CHARS")
    if max_chars_env is None or str(max_chars_env).strip() == "":
        emb = str(os.environ.get("EMBEDDING_MODEL", "") or "").strip().lower()
        default_max_chars = 32000 if "bge-m3" in emb else 8000
        max_chars = default_max_chars
    else:
        max_chars = int(max_chars_env)
    max_chars = max(256, min(max_chars, 100000))

    # Normalize mode aliases and allow additive composition: "code+info+pseudo+tags".
    alias = {
        "information": "info",
        "document": "info",
        "text": "info",
        "doc": "info",
        "docs": "info",
    }
    if not mode:
        mode = "code+info"
    raw_parts = [p for p in mode.replace(",", "+").split("+") if p.strip()]
    parts = {alias.get(p.strip().lower(), p.strip().lower()) for p in raw_parts}

    # Shorthand: mode=="info" (and aliases) means info-only.
    if parts == {"info"}:
        include_code = False
        include_info = True
        include_pseudo = False
        include_tags = False
    else:
        _recognized = {"code", "info", "pseudo", "tags"}
        _unknown_only = bool(parts) and parts.isdisjoint(_recognized)
        include_code = ("code" in parts) or (not parts) or _unknown_only  # default to code when unknown
        include_info = ("info" in parts)
        include_pseudo = ("pseudo" in parts)
        include_tags = ("tags" in parts)

    def _normalize_info_for_dense(s: str) -> str:
        s = (s or "").strip()
        if not s:
            return ""
        try:
            import re as _re
            # Strip unstable line-range numbers: "lines 12-34" → keeps path/language stable.
            s = _re.sub(r"\s+lines\s+\d+\s*-\s*\d+\.?", ".", s, flags=_re.IGNORECASE)
            s = _re.sub(r"\s+\.\s+", ". ", s)
        except Exception as e:
            logger.debug(f"Suppressed exception: {e}")
        return s.strip()

    header: list[str] = []
    if include_info:
        info_norm = _normalize_info_for_dense(info)
        if info_norm:
            header.append(info_norm)
    if include_pseudo and pseudo:
        header.append(str(pseudo).strip())
    if include_tags and tags:
        clean_tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        if clean_tags:
            header.append(" ".join(clean_tags[:12]))

    body = ""
    if include_code:
        body = (code_text or "").strip()
        if not body and not header:
            body = _normalize_info_for_dense(info)

    # Compose. Put structured context first, then raw code.
    pieces = [p for p in header if p]
    if body:
        if pieces:
            pieces.append("")  # blank line separator
        pieces.append(body)
    text = "\n".join(pieces).strip()

    # Fallback: if no semantic content (no pseudo/tags), include text body.
    # This ensures text files without pseudo generation still get meaningful embeddings.
    has_semantic = bool(pseudo) or bool(tags)
    if not text or (not has_semantic and not include_code):
        # No semantic enrichment available - fall back to text content
        fallback = (code_text or "").strip()
        if fallback:
            info_norm = _normalize_info_for_dense(info)
            text = f"{info_norm}\n\n{fallback}" if info_norm else fallback
        elif not text:
            text = _normalize_info_for_dense(info)
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars]
    return text


def _select_entity_text(
    *,
    symbol: str = "",
    symbol_path: str = "",
    kind: str = "",
    signature: str = "",
    docstring: str = "",
    parent: str = "",
) -> str:
    """Build text for entity_dense embedding (symbol signatures).

    This embeds the "what" of the code: function/class names, signatures, types.
    Format: "{kind} {name}: {signature}. {docstring_first_line}"
    """
    parts = []

    # Kind and name
    kind_str = (kind or "code").strip()
    name = (symbol or "").strip()
    if symbol_path and symbol_path != name:
        name = symbol_path.strip()

    if name:
        parts.append(f"{kind_str} {name}")

    # Signature (function definition, class inheritance)
    sig = (signature or "").strip()
    if sig:
        # Truncate long signatures
        if len(sig) > 200:
            sig = sig[:200] + "..."
        parts.append(sig)

    # First line of docstring (intent summary)
    doc = (docstring or "").strip()
    if doc:
        first_line = doc.split("\n")[0].strip()
        if len(first_line) > 150:
            first_line = first_line[:150] + "..."
        if first_line:
            parts.append(first_line)

    # Parent context (e.g., "in class UserService")
    if parent:
        parts.append(f"in {parent}")

    text = ". ".join(parts) if parts else ""
    return text[:1000] if text else ""  # Cap at 1000 chars


def _select_relation_text(
    *,
    symbol: str = "",
    calls: list[str] | None = None,
    imports: list[str] | None = None,
) -> str:
    """Build text for relation_dense embedding (call/import relationships).

    This embeds the "how" code connects: what it calls, what it imports.
    Format: "{symbol} calls {callees}; imports {modules}"

    Relation embeddings enable graph-aware retrieval.
    """
    parts = []

    # Symbol context
    sym = (symbol or "").strip()
    if sym:
        parts.append(sym)

    # Calls (what this code invokes)
    call_list = [str(c).strip() for c in (calls or []) if str(c).strip()]
    if call_list:
        # Limit to first 20 calls to keep embedding focused
        calls_str = ", ".join(call_list[:20])
        if len(call_list) > 20:
            calls_str += f" (+{len(call_list) - 20} more)"
        parts.append(f"calls {calls_str}")

    # Imports (dependencies)
    import_list = [str(i).strip() for i in (imports or []) if str(i).strip()]
    if import_list:
        # Limit to first 15 imports
        imports_str = ", ".join(import_list[:15])
        if len(import_list) > 15:
            imports_str += f" (+{len(import_list) - 15} more)"
        parts.append(f"imports {imports_str}")

    text = "; ".join(parts) if parts else ""
    return text[:800] if text else ""  # Cap at 800 chars


def build_information(
    language: str, path: Path, start: int, end: int, first_line: str
) -> str:
    """Build the information/document string for a chunk."""
    first_line = (first_line or "").strip()
    if len(first_line) > 160:
        first_line = first_line[:160] + "…"
    return f"{language} code from {path} lines {start}-{end}. {first_line}"


def index_single_file(
    client: QdrantClient,
    model: "TextEmbedding",
    collection: str,
    vector_name: str,
    file_path: Path,
    *,
    dedupe: bool = True,
    skip_unchanged: bool = True,
    pseudo_mode: str = "full",
    trust_cache: bool | None = None,
    repo_name_for_cache: str | None = None,
    allowed_vectors: set[str] | None = None,
    allowed_sparse: set[str] | None = None,
) -> bool:
    """Index a single file path. Returns True if indexed, False if skipped."""
    try:
        if _should_skip_explicit_file_by_excluder(file_path):
            try:
                delete_points_by_path(client, collection, str(file_path))
            except Exception as e:
                logger.debug(f"Suppressed exception: {e}")
            print(f"Skipping excluded file: {file_path}")
            return False
    except Exception:
        return False

    _file_lock_ctx = None
    if file_indexing_lock is not None:
        try:
            _file_lock_ctx = file_indexing_lock(str(file_path))
            _file_lock_ctx.__enter__()
        except FileExistsError:
            print(f"[FILE_LOCKED] Skipping {file_path} - another process is indexing it")
            return False
        except Exception as e:
            logger.debug(f"Suppressed exception: {e}")

    try:
        return _index_single_file_inner(
            client, model, collection, vector_name, file_path,
            dedupe=dedupe, skip_unchanged=skip_unchanged, pseudo_mode=pseudo_mode,
            trust_cache=trust_cache,
            repo_name_for_cache=repo_name_for_cache,
            allowed_vectors=allowed_vectors,
            allowed_sparse=allowed_sparse,
        )
    finally:
        if _file_lock_ctx is not None:
            try:
                _file_lock_ctx.__exit__(None, None, None)
            except Exception as e:
                logger.debug(f"Suppressed exception: {e}")


def _index_single_file_inner(
    client: QdrantClient,
    model: "TextEmbedding",
    collection: str,
    vector_name: str,
    file_path: Path,
    *,
    dedupe: bool = True,
    skip_unchanged: bool = True,
    pseudo_mode: str = "full",
    trust_cache: bool | None = None,
    repo_name_for_cache: str | None = None,
    allowed_vectors: set[str] | None = None,
    allowed_sparse: set[str] | None = None,
) -> bool:
    """Inner implementation of index_single_file (after lock is acquired)."""
    if trust_cache is None:
        try:
            trust_cache = os.environ.get("INDEX_TRUST_CACHE", "").strip().lower() in {
                "1", "true", "yes", "on",
            }
        except Exception:
            trust_cache = False

    fast_fs = _env_truthy(os.environ.get("INDEX_FS_FASTPATH"), False)
    if skip_unchanged and fast_fs and get_cached_file_meta is not None:
        try:
            repo_for_cache = repo_name_for_cache or _detect_repo_name_from_path(file_path)
            meta = get_cached_file_meta(str(file_path), repo_for_cache) or {}
            size = meta.get("size")
            mtime = meta.get("mtime")
            if size is not None and mtime is not None:
                st = file_path.stat()
                if int(getattr(st, "st_size", 0)) == int(size) and int(
                    getattr(st, "st_mtime", 0)
                ) == int(mtime):
                    print(f"Skipping unchanged file (fs-meta): {file_path}")
                    return False
        except Exception as e:
            logger.debug(f"Suppressed exception: {e}")

    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"Skipping {file_path}: {e}")
        return False

    language = detect_language(file_path)
    is_text_like = _is_text_like_language(language)
    file_hash = xxhash.xxh64(text.encode("utf-8", errors="ignore")).hexdigest()

    repo_tag = repo_name_for_cache or _detect_repo_name_from_path(file_path)

    repo_id: str | None = None
    repo_rel_path: str | None = None
    if logical_repo_reuse_enabled() and get_workspace_state is not None:
        try:
            ws_root = os.environ.get("WATCH_ROOT") or os.environ.get("WORKSPACE_PATH") or "/work"
            state = get_workspace_state(ws_root, repo_tag)
            lrid = state.get("logical_repo_id") if isinstance(state, dict) else None
            if isinstance(lrid, str) and lrid:
                repo_id = lrid
            try:
                fp = file_path.resolve()
            except Exception:
                fp = file_path
            try:
                ws_base = Path(os.environ.get("WATCH_ROOT") or os.environ.get("WORKSPACE_PATH") or "/work").resolve()
                repo_root = ws_base
                if repo_tag:
                    candidate = ws_base / repo_tag
                    if candidate.exists():
                        repo_root = candidate
                rel = fp.relative_to(repo_root)
                repo_rel_path = rel.as_posix()
            except Exception:
                repo_rel_path = None
        except Exception as e:
            print(f"[logical_repo] Failed to derive logical identity for {file_path}: {e}")

    changed_symbols = set()
    if get_cached_symbols and set_cached_symbols:
        cached_symbols = get_cached_symbols(str(file_path))
        if cached_symbols:
            current_symbols = extract_symbols_with_tree_sitter(str(file_path), content=text, language=language)
            _, changed = compare_symbol_changes(cached_symbols, current_symbols)
            for symbol_data in current_symbols.values():
                symbol_id = f"{symbol_data['type']}_{symbol_data['name']}_{symbol_data['start_line']}"
                if symbol_id in changed:
                    changed_symbols.add(symbol_id)

    if skip_unchanged:
        ws_path = os.environ.get("WATCH_ROOT") or os.environ.get("WORKSPACE_PATH") or "/work"
        try:
            if get_cached_file_hash:
                prev_local = get_cached_file_hash(str(file_path), repo_tag)
                if prev_local and file_hash and prev_local == file_hash:
                    if fast_fs and set_cached_file_hash:
                        try:
                            set_cached_file_hash(str(file_path), file_hash, repo_tag)
                        except Exception as e:
                            logger.debug(f"Suppressed exception: {e}")
                    print(f"Skipping unchanged file (cache): {file_path}")
                    return False
        except Exception as e:
            logger.debug(f"Suppressed exception: {e}")

        if not trust_cache:
            prev = get_indexed_file_hash(
                client, collection, str(file_path),
                repo_id=repo_id, repo_rel_path=repo_rel_path,
            )
            if prev and prev == file_hash:
                if fast_fs and set_cached_file_hash:
                    try:
                        set_cached_file_hash(str(file_path), file_hash, repo_tag)
                    except Exception as e:
                        logger.debug(f"Suppressed exception: {e}")
                print(f"Skipping unchanged file: {file_path}")
                return False

    if dedupe:
        delete_points_by_path(client, collection, str(file_path))

    ast_info = _ast_analyze_file(file_path, language, text)
    symbols = ast_info.get("symbol_spans") or _extract_symbols(language, text)
    imports = ast_info.get("imports")
    calls = ast_info.get("calls")
    # Always get import_map for callee resolution (ast_info doesn't provide it)
    _, _, import_map = _get_imports_calls(language, text)
    # Get class inheritance relationships
    inheritance_map = _get_inheritance(language, text)
    if "imports" not in ast_info or "calls" not in ast_info:
        base_imports, base_calls, _ = _get_imports_calls(language, text)
        if "imports" not in ast_info:
            imports = base_imports
        if "calls" not in ast_info:
            calls = base_calls
    symbol_meta_by_path = ast_info.get("symbol_meta_by_path", {})
    symbol_meta_by_name = ast_info.get("symbol_meta_by_name", {})
    ast_call_refs = ast_info.get("call_refs", [])
    ast_import_refs = ast_info.get("import_refs", [])
    symbol_calls = ast_info.get("symbol_calls", {})
    last_mod, churn_count, author_count = _git_metadata(file_path)

    CHUNK_LINES = int(os.environ.get("INDEX_CHUNK_LINES", "120") or 120)
    CHUNK_OVERLAP = int(os.environ.get("INDEX_CHUNK_OVERLAP", "20") or 20)
    use_micro = os.environ.get("INDEX_MICRO_CHUNKS", "0").lower() in {"1", "true", "yes", "on"}
    use_semantic = os.environ.get("INDEX_SEMANTIC_CHUNKS", "1").lower() in {"1", "true", "yes", "on"}
    use_sdc = os.environ.get("INDEX_SDC_CHUNKS", "0").lower() in {"1", "true", "yes", "on"}
    use_sosc = os.environ.get("INDEX_SOSC_CHUNKS", "0").lower() in {"1", "true", "yes", "on"}
    use_cast = os.environ.get("INDEX_CAST_CHUNKS", "0").lower() in {"1", "true", "yes", "on"}

    if use_micro:
        try:
            _cap = int(os.environ.get("MAX_MICRO_CHUNKS_PER_FILE", "200") or 200)
            _base_tokens = int(os.environ.get("MICRO_CHUNK_TOKENS", "128") or 128)
            _base_stride = int(os.environ.get("MICRO_CHUNK_STRIDE", "64") or 64)
            chunks = chunk_by_tokens(text, k_tokens=_base_tokens, stride_tokens=_base_stride)
            if _cap > 0 and len(chunks) > _cap:
                _before = len(chunks)
                _scale = (len(chunks) / _cap) * 1.1
                _new_tokens = max(_base_tokens, int(_base_tokens * _scale))
                _new_stride = max(_base_stride, int(_base_stride * _scale))
                chunks = chunk_by_tokens(text, k_tokens=_new_tokens, stride_tokens=_new_stride)
                try:
                    print(
                        f"[ingest] micro-chunks resized path={file_path} count={_before}->{len(chunks)} "
                        f"tokens={_base_tokens}->{_new_tokens} stride={_base_stride}->{_new_stride}"
                    )
                except Exception as e:
                    logger.debug(f"Suppressed exception: {e}")
        except Exception:
            chunks = chunk_by_tokens(text)
    elif use_sosc:
        chunks = chunk_search_optimized_v1(text, language)
    elif use_cast:
        chunks = chunk_cast_plus_v1(text, language)
    elif use_sdc:
        chunks = chunk_semantic_v2(text, language)
    elif use_semantic:
        chunks = chunk_semantic(text, language, CHUNK_LINES, CHUNK_OVERLAP)
    else:
        chunks = chunk_lines(text, CHUNK_LINES, CHUNK_OVERLAP)

    batch_texts: List[str] = []
    batch_meta: List[Dict] = []
    batch_ids: List[int] = []
    batch_lex: List[list[float]] = []
    batch_lex_text: List[str] = []
    batch_code: List[str] = []  # Raw code for pattern vectors
    # Multi-granular vector batches
    batch_entity_texts: List[str] = []  # For entity_dense vectors
    batch_relation_texts: List[str] = []  # For relation_dense vectors

    if allowed_vectors is None and allowed_sparse is None:
        allowed_vectors, allowed_sparse = get_collection_vector_names(client, collection)

    allow_lex = allowed_vectors is None or LEX_VECTOR_NAME in allowed_vectors
    allow_mini = allowed_vectors is None or MINI_VECTOR_NAME in allowed_vectors
    allow_pattern = allowed_vectors is None or PATTERN_VECTOR_NAME in allowed_vectors
    allow_sparse = allowed_sparse is None or LEX_SPARSE_NAME in allowed_sparse
    # Multi-granular vector support: only enable if collection actually has these vectors
    allow_entity = allowed_vectors is None or ENTITY_DENSE_NAME in allowed_vectors
    allow_relation = allowed_vectors is None or RELATION_DENSE_NAME in allowed_vectors

    # Check if pattern vectors are enabled
    pattern_vectors_on = os.environ.get("PATTERN_VECTORS", "").strip().lower() in {"1", "true", "yes", "on"}
    pattern_vectors_on = pattern_vectors_on and allow_pattern
    refrag_on = os.environ.get("REFRAG_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    use_mini = refrag_on and allow_mini
    use_sparse = LEX_SPARSE_MODE and allow_sparse

    # Check if multi-granular vectors are enabled AND collection supports them
    # This gates writes on allowed_vectors to avoid upsert failures on unknown vector names
    use_multi_granular = MULTI_GRANULAR_VECTORS and allow_entity and allow_relation

    def make_point(
        pid, dense_vec, lex_vec, payload, lex_text: str = "", code_text: str = "",
        entity_dense_vec: list | None = None, relation_dense_vec: list | None = None,
    ):
        if vector_name:
            vecs = {vector_name: dense_vec}
            if allow_lex:
                vecs[LEX_VECTOR_NAME] = lex_vec
            try:
                if use_mini:
                    vecs[MINI_VECTOR_NAME] = project_mini(list(dense_vec), MINI_VEC_DIM)
            except Exception as e:
                logger.debug(f"Suppressed exception: {e}")
            # Add pattern vector for structural similarity search
            if pattern_vectors_on and code_text:
                try:
                    pv = extract_pattern_vector(code_text, language)
                    if pv:
                        vecs[PATTERN_VECTOR_NAME] = pv
                except Exception as e:
                    logger.debug(f"Suppressed exception: {e}")
            if use_sparse and lex_text:
                sparse_vec = _lex_sparse_vector_text(lex_text)
                if sparse_vec.get("indices"):
                    vecs[LEX_SPARSE_NAME] = models.SparseVector(**sparse_vec)
            # Add multi-granular vectors
            if use_multi_granular:
                if entity_dense_vec is not None:
                    vecs[ENTITY_DENSE_NAME] = entity_dense_vec
                if relation_dense_vec is not None:
                    vecs[RELATION_DENSE_NAME] = relation_dense_vec
            return models.PointStruct(id=pid, vector=vecs, payload=payload)
        else:
            return models.PointStruct(id=pid, vector=dense_vec, payload=payload)

    pseudo_batch_concurrency = int(os.environ.get("PSEUDO_BATCH_CONCURRENCY", "1") or 1)
    use_batch_pseudo = pseudo_batch_concurrency > 1 and pseudo_mode == "full"

    chunk_data: list[dict] = []
    # Mapping from symbol_path to point_id for graph edge extraction
    symbol_path_to_point_id: dict[str, str] = {}
    for ch in chunks:
        info = build_information(
            language, file_path, ch["start"], ch["end"],
            ch["text"].splitlines()[0] if ch["text"] else "",
        )
        kind, sym, sym_path = _choose_symbol_for_chunk(ch["start"], ch["end"], symbols)
        if "kind" in ch and ch.get("kind"):
            kind = ch.get("kind") or kind
        if "symbol" in ch and ch.get("symbol"):
            sym = ch.get("symbol") or sym
        if "symbol_path" in ch and ch.get("symbol_path"):
            sym_path = ch.get("symbol_path") or sym_path
        if not ch.get("kind") and kind:
            ch["kind"] = kind
        if not ch.get("symbol") and sym:
            ch["symbol"] = sym
        if not ch.get("symbol_path") and sym_path:
            ch["symbol_path"] = sym_path

        if not ch.get("calls") and ast_call_refs:
            ch["calls"] = _slice_calls_for_range(ast_call_refs, ch["start"], ch["end"])
        if not ch.get("imports") and ast_import_refs:
            ch["imports"] = _slice_imports_for_range(ast_import_refs, ch["start"], ch["end"])

        symbol_info = None
        if sym_path:
            symbol_info = symbol_meta_by_path.get(sym_path)
        if symbol_info is None and sym:
            symbol_info = symbol_meta_by_name.get(sym)
        if symbol_info is not None:
            ch["symbol_start_line"] = int(getattr(symbol_info, "start_line", 0) or 0)
            ch["symbol_end_line"] = int(getattr(symbol_info, "end_line", 0) or 0)
            signature = str(getattr(symbol_info, "signature", "") or "")
            docstring = str(getattr(symbol_info, "docstring", "") or "")
            parent = str(getattr(symbol_info, "parent", "") or "")
            if signature:
                ch["symbol_signature"] = signature
            if docstring:
                ch["symbol_docstring"] = docstring
            if parent:
                ch["symbol_parent"] = parent

        _cur_path = str(file_path)
        _host_path, _container_path = _compute_host_and_container_paths(_cur_path)

        payload = {
            "document": info,
            "information": info,
            "metadata": {
                "path": str(file_path),
                "path_prefix": str(file_path.parent),
                "ext": str(file_path.suffix).lstrip(".").lower(),
                "language": language,
                "kind": kind,
                "symbol": sym,
                "symbol_path": sym_path,
                "repo": repo_tag,
                "start_line": ch["start"],
                "end_line": ch["end"],
                "code": ch["text"],
                "file_hash": file_hash,
                # Use chunk-specific calls/imports when available (semantic chunks),
                # otherwise fall back to file-level calls/imports
                "imports": ch.get("imports") if ch.get("imports") else imports,
                "calls": ch.get("calls") if ch.get("calls") else calls,
                # Import map for callee resolution: local_name -> qualified_path
                "import_map": import_map if import_map else None,
                "inheritance_map": inheritance_map if inheritance_map else None,
                "symbol_start_line": ch.get("symbol_start_line"),
                "symbol_end_line": ch.get("symbol_end_line"),
                "symbol_signature": ch.get("symbol_signature"),
                "symbol_docstring": ch.get("symbol_docstring"),
                "symbol_parent": ch.get("symbol_parent"),
                "ingested_at": int(time.time()),
                "last_modified_at": int(last_mod),
                "churn_count": int(churn_count),
                "author_count": int(author_count),
                "repo_id": repo_id,
                "repo_rel_path": repo_rel_path,
                "host_path": _host_path,
                "container_path": _container_path,
            },
        }

        needs_pseudo_gen = False
        cached_pseudo, cached_tags = "", []
        if pseudo_mode != "off":
            needs_pseudo_gen, cached_pseudo, cached_tags = should_process_pseudo_for_chunk(
                str(file_path), ch, changed_symbols
            )

        chunk_data.append({
            "chunk": ch,
            "info": info,
            "payload": payload,
            "kind": kind,
            "needs_pseudo": needs_pseudo_gen and pseudo_mode == "full",
            "cached_pseudo": cached_pseudo,
            "cached_tags": cached_tags,
        })

    if use_batch_pseudo:
        pending_indices = [i for i, cd in enumerate(chunk_data) if cd["needs_pseudo"]]
        pending_texts = [chunk_data[i]["chunk"].get("text") or "" for i in pending_indices]

        if pending_texts:
            try:
                from scripts.refrag_glm import generate_pseudo_tags_batch
                batch_results = generate_pseudo_tags_batch(pending_texts, concurrency=pseudo_batch_concurrency)
                for idx, (pseudo, tags) in zip(pending_indices, batch_results):
                    chunk_data[idx]["cached_pseudo"] = pseudo
                    chunk_data[idx]["cached_tags"] = tags
                    chunk_data[idx]["needs_pseudo"] = False
                    if pseudo or tags:
                        ch = chunk_data[idx]["chunk"]
                        symbol_name = ch.get("symbol", "")
                        if symbol_name and set_cached_pseudo:
                            k = ch.get("kind", "unknown")
                            start_line = ch.get("start", 0)
                            symbol_id = f"{k}_{symbol_name}_{start_line}"
                            set_cached_pseudo(str(file_path), symbol_id, pseudo, tags, file_hash)
            except Exception as e:
                print(f"[PSEUDO_BATCH] Batch failed, falling back to sequential: {e}")
                use_batch_pseudo = False

    for cd in chunk_data:
        ch = cd["chunk"]
        payload = cd["payload"]
        pseudo = cd["cached_pseudo"]
        tags = cd["cached_tags"]

        if not use_batch_pseudo and cd["needs_pseudo"]:
            try:
                pseudo, tags = generate_pseudo_tags(ch.get("text") or "")
                if pseudo or tags:
                    symbol_name = ch.get("symbol", "")
                    if symbol_name:
                        kind = ch.get("kind", "unknown")
                        start_line = ch.get("start", 0)
                        symbol_id = f"{kind}_{symbol_name}_{start_line}"
                        if set_cached_pseudo:
                            set_cached_pseudo(str(file_path), symbol_id, pseudo, tags, file_hash)
            except Exception as e:
                logger.debug(f"Suppressed exception: {e}")

        if pseudo:
            payload["pseudo"] = pseudo
        if tags:
            payload["tags"] = tags
        dense_mode = (
            str(os.environ.get("INDEX_DENSE_MODE", "info+pseudo+tags") or "")
            .strip()
            .lower()
            or "info+pseudo+tags"
        )
        payload["dense_mode"] = dense_mode
        dense_text = _select_dense_text(
            info=cd["info"],
            code_text=ch.get("text") or "",
            pseudo=pseudo,
            tags=tags,
            mode=dense_mode,
        )
        batch_texts.append(dense_text)
        batch_meta.append(payload)
        point_id = hash_id(ch["text"], str(file_path), ch["start"], ch["end"])
        batch_ids.append(point_id)
        # Track symbol_path -> point_id for graph edge extraction
        chunk_symbol_path = ch.get("symbol_path") or ""
        if chunk_symbol_path:
            symbol_path_to_point_id[chunk_symbol_path] = str(point_id)
        aug_lex_text = (ch.get("text") or "") + (" " + pseudo if pseudo else "") + (" " + " ".join(tags) if tags else "")
        batch_lex.append(_lex_hash_vector_text(aug_lex_text))
        batch_lex_text.append(aug_lex_text)
        batch_code.append(ch.get("text") or "")

        # Generate multi-granular vector texts
        if use_multi_granular:
            entity_text = _select_entity_text(
                symbol=ch.get("symbol") or "",
                symbol_path=ch.get("symbol_path") or "",
                kind=ch.get("kind") or "",
                signature=ch.get("symbol_signature") or "",
                docstring=ch.get("symbol_docstring") or "",
                parent=ch.get("symbol_parent") or "",
            )
            relation_text = _select_relation_text(
                symbol=ch.get("symbol") or "",
                calls=ch.get("calls") or payload.get("metadata", {}).get("calls") or [],
                imports=ch.get("imports") or payload.get("metadata", {}).get("imports") or [],
            )
            batch_entity_texts.append(entity_text)
            batch_relation_texts.append(relation_text)
        else:
            batch_entity_texts.append("")
            batch_relation_texts.append("")

    if batch_texts:
        vectors = embed_batch(model, batch_texts)

        # Embed multi-granular vectors if enabled
        entity_vectors: List[list] = []
        relation_vectors: List[list] = []
        if use_multi_granular and batch_entity_texts:
            # Filter non-empty texts to embed, track indices
            entity_to_embed = [(i, t) for i, t in enumerate(batch_entity_texts) if t.strip()]
            relation_to_embed = [(i, t) for i, t in enumerate(batch_relation_texts) if t.strip()]

            # Initialize with None for all positions
            entity_vectors = [None] * len(batch_entity_texts)
            relation_vectors = [None] * len(batch_relation_texts)

            # Embed entity texts
            if entity_to_embed:
                try:
                    entity_texts_only = [t for _, t in entity_to_embed]
                    entity_vecs = embed_batch(model, entity_texts_only)
                    for (orig_idx, _), vec in zip(entity_to_embed, entity_vecs):
                        entity_vectors[orig_idx] = vec
                except Exception as e:
                    logger.warning(f"[MULTI_GRANULAR] Entity embedding failed: {e}")

            # Embed relation texts
            if relation_to_embed:
                try:
                    relation_texts_only = [t for _, t in relation_to_embed]
                    relation_vecs = embed_batch(model, relation_texts_only)
                    for (orig_idx, _), vec in zip(relation_to_embed, relation_vecs):
                        relation_vectors[orig_idx] = vec
                except Exception as e:
                    logger.warning(f"[MULTI_GRANULAR] Relation embedding failed: {e}")
        else:
            # Pad with None if not using multi-granular
            entity_vectors = [None] * len(batch_texts)
            relation_vectors = [None] * len(batch_texts)

        for _idx, _m in enumerate(batch_meta):
            try:
                _m["pid_str"] = str(batch_ids[_idx])
            except Exception as e:
                logger.debug(f"Suppressed exception: {e}")
        points = [
            make_point(i, v, lx, m, lt, ct, entity_dense_vec=ev, relation_dense_vec=rv)
            for i, v, lx, m, lt, ct, ev, rv in zip(
                batch_ids, vectors, batch_lex, batch_meta, batch_lex_text, batch_code,
                entity_vectors, relation_vectors
            )
        ]
        upsert_points(client, collection, points)

        # Emit graph edges for symbol relationships (always on - Qdrant flat graph)
        # Neo4j takes precedence when NEO4J_GRAPH=1 is set
        # Always try symbol-level edges first, fall back to file-level if no symbol_calls
        try:
            graph_coll = ensure_graph_collection(client, collection)
            # Delete old edges for this file before upserting new ones
            delete_edges_by_path(client, graph_coll, str(file_path), repo=repo_tag)

            all_edges = []
            if symbol_calls:
                # Symbol-level edges: use AST-extracted caller→callee relationships
                for caller, callees in symbol_calls.items():
                    if not caller or not callees:
                        continue
                    start_line = None
                    end_line = None
                    sym_info = symbol_meta_by_path.get(caller) or symbol_meta_by_name.get(caller)
                    if sym_info is not None:
                        try:
                            start_line = int(getattr(sym_info, "start_line", 0) or 0)
                            end_line = int(getattr(sym_info, "end_line", 0) or 0)
                        except Exception:
                            start_line = None
                            end_line = None
                    # Get caller_point_id from symbol_path mapping
                    caller_pid = symbol_path_to_point_id.get(caller)
                    all_edges.extend(
                        _extract_call_edges_compat(
                            symbol_path=caller,
                            calls=callees,
                            path=str(file_path),
                            repo=repo_tag,
                            start_line=start_line,
                            end_line=end_line,
                            language=language,
                            caller_point_id=caller_pid,
                            import_paths=import_map,
                            collection=collection,
                            qdrant_client=client,
                        )
                    )
                if imports:
                    # For file-level imports, use first point ID if available
                    file_pid = next(iter(symbol_path_to_point_id.values()), None) if symbol_path_to_point_id else None
                    all_edges.extend(
                        _extract_import_edges_compat(
                            symbol_path=str(file_path),
                            imports=imports,
                            path=str(file_path),
                            repo=repo_tag,
                            language=language,
                            caller_point_id=file_pid,
                            collection=collection,
                            qdrant_client=client,
                        )
                    )
            else:
                # File-level fallback: emit file→symbol edges
                source_file_path = str(file_path)
                # Use first point ID for file-level edges
                file_pid = next(iter(symbol_path_to_point_id.values()), None) if symbol_path_to_point_id else None
                if calls:
                    all_edges.extend(_extract_call_edges_compat(
                        symbol_path=source_file_path,
                        calls=calls,
                        path=source_file_path,
                        repo=repo_tag,
                        caller_point_id=file_pid,
                        import_paths=import_map,
                        collection=collection,
                        qdrant_client=client,
                    ))
                if imports:
                    all_edges.extend(_extract_import_edges_compat(
                        symbol_path=source_file_path,
                        imports=imports,
                        path=source_file_path,
                        repo=repo_tag,
                        caller_point_id=file_pid,
                        collection=collection,
                        qdrant_client=client,
                    ))

            # Extract inheritance edges (INHERITS_FROM) for all classes
            if inheritance_map:
                for class_name, base_classes in inheritance_map.items():
                    if class_name and base_classes:
                        all_edges.extend(extract_inheritance_edges(
                            class_name=class_name,
                            base_classes=base_classes,
                            path=str(file_path),
                            repo=repo_tag,
                            language=language,
                            import_paths=import_map,
                            collection=collection,
                            qdrant_client=client,
                        ))

            if all_edges:
                upsert_edges(client, graph_coll, all_edges)
        except Exception as e:
            # Don't fail indexing if graph edges fail
            logger.warning(f"Failed to emit graph edges for {file_path}: {e}")

        try:
            ws = os.environ.get("WATCH_ROOT") or os.environ.get("WORKSPACE_PATH") or "/work"
            if set_cached_file_hash:
                file_repo_tag = repo_tag
                set_cached_file_hash(str(file_path), file_hash, file_repo_tag)
        except Exception as e:
            logger.debug(f"Suppressed exception: {e}")
        return True
    return False


def index_repo(
    root: Path,
    qdrant_url: str,
    api_key: str,
    collection: str,
    model_name: str,
    recreate: bool,
    *,
    dedupe: bool = True,
    skip_unchanged: bool = True,
    pseudo_mode: str = "full",
    schema_mode: str | None = None,
):
    """Index a repository into Qdrant."""
    # CRITICAL OPTIMIZATION: When recreating collection, skip all cache checks and deduplication
    # The collection is empty, so:
    # - skip_unchanged=False: no point checking if file hash changed (nothing in DB)
    # - dedupe=False: no point deleting existing points (collection is empty)
    # This avoids 2 Qdrant calls per file (scroll + delete) that are wasteful for fresh collections
    if recreate:
        skip_unchanged = False
        dedupe = False
        logger.info("[index_repo] Recreate mode: skipping cache checks and deduplication (collection is fresh)")

    fast_fs = _env_truthy(os.environ.get("INDEX_FS_FASTPATH"), False)
    if skip_unchanged and not recreate and fast_fs and get_cached_file_meta is not None:
        try:
            is_multi_repo = bool(is_multi_repo_mode and is_multi_repo_mode())
            root_repo_for_cache = (
                _detect_repo_name_from_path(root)
                if (not is_multi_repo and _detect_repo_name_from_path)
                else None
            )
            all_unchanged = True
            for file_path in iter_files(root):
                per_file_repo_for_cache = (
                    root_repo_for_cache
                    if root_repo_for_cache is not None
                    else (
                        _detect_repo_name_from_path(file_path)
                        if _detect_repo_name_from_path
                        else None
                    )
                )
                meta = get_cached_file_meta(str(file_path), per_file_repo_for_cache) or {}
                size = meta.get("size")
                mtime = meta.get("mtime")
                if size is None or mtime is None:
                    all_unchanged = False
                    break
                st = file_path.stat()
                if int(getattr(st, "st_size", 0)) != int(size) or int(getattr(st, "st_mtime", 0)) != int(mtime):
                    all_unchanged = False
                    break
            if all_unchanged:
                try:
                    print("[fast_index] No changes detected via fs metadata; skipping model and Qdrant setup")
                except Exception as e:
                    logger.debug(f"Suppressed exception: {e}")
                return
        except Exception as e:
            logger.debug(f"Suppressed exception: {e}")

    try:
        from scripts.embedder import get_embedding_model, get_model_dimension
        model = get_embedding_model(model_name)
        dim = get_model_dimension(model_name)
    except ImportError:
        from fastembed import TextEmbedding
        model = TextEmbedding(model_name=model_name)
        dim = len(next(model.embed(["dimension probe"])))

    client = QdrantClient(
        url=qdrant_url,
        api_key=api_key or None,
        timeout=int(os.environ.get("QDRANT_TIMEOUT", "20") or 20),
    )

    if recreate:
        vector_name = _sanitize_vector_name(model_name)
    else:
        vector_name = None
        try:
            info = client.get_collection(collection)
            cfg = info.config.params.vectors
            if isinstance(cfg, dict) and cfg:
                for name, params in cfg.items():
                    psize = getattr(params, "size", None) or getattr(params, "dim", None)
                    if psize and int(psize) == int(dim):
                        vector_name = name
                        break
                if vector_name is None and LEX_VECTOR_NAME in cfg:
                    for name in cfg.keys():
                        if name != LEX_VECTOR_NAME:
                            vector_name = name
                            break
        except Exception as e:
            logger.debug(f"Suppressed exception: {e}")
        if vector_name is None:
            vector_name = _sanitize_vector_name(model_name)

    if recreate:
        recreate_collection(client, collection, dim, vector_name)

    ensure_mode = schema_mode
    mode_value = (ensure_mode or "legacy").strip().lower()
    if recreate and mode_value in {"validate", "create"}:
        ensure_mode = "migrate"
        mode_value = "migrate"

    try:
        ensure_collection_and_indexes_once(
            client,
            collection,
            dim,
            vector_name,
            schema_mode=ensure_mode,
        )
    except Exception:
        if mode_value in {"validate", "create"}:
            raise
        ensure_collection(
            client,
            collection,
            dim,
            vector_name,
            schema_mode=ensure_mode,
        )
        if mode_value in {"legacy", "migrate"}:
            ensure_payload_indexes(client, collection)

    allowed_vectors, allowed_sparse = get_collection_vector_names(client, collection)
    if allowed_vectors is not None and vector_name and vector_name not in allowed_vectors:
        print(
            f"[COLLECTION_WARNING] Collection {collection} missing dense vector '{vector_name}'. "
            "Indexing may fail until schema is updated."
        )
    if allowed_vectors is not None:
        refrag_on = os.environ.get("REFRAG_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
        if refrag_on and MINI_VECTOR_NAME not in allowed_vectors:
            print(
                f"[COLLECTION_WARNING] Collection {collection} lacks mini vector '{MINI_VECTOR_NAME}'. "
                "ReFRAG vectors will be skipped for this run."
            )
        pattern_on = os.environ.get("PATTERN_VECTORS", "").strip().lower() in {"1", "true", "yes", "on"}
        if pattern_on and PATTERN_VECTOR_NAME not in allowed_vectors:
            print(
                f"[COLLECTION_WARNING] Collection {collection} lacks pattern vector '{PATTERN_VECTOR_NAME}'. "
                "Pattern vectors will be skipped for this run."
            )
        if LEX_VECTOR_NAME not in allowed_vectors:
            print(
                f"[COLLECTION_WARNING] Collection {collection} lacks lexical vector '{LEX_VECTOR_NAME}'. "
                "Lexical vectors will be skipped for this run."
            )
    if allowed_sparse is not None and LEX_SPARSE_MODE and LEX_SPARSE_NAME not in allowed_sparse:
        print(
            f"[COLLECTION_WARNING] Collection {collection} lacks sparse vector '{LEX_SPARSE_NAME}'. "
            "Sparse vectors will be skipped for this run."
        )

    is_multi_repo = bool(is_multi_repo_mode and is_multi_repo_mode())
    root_repo_for_cache = (
        _detect_repo_name_from_path(root)
        if (not is_multi_repo and _detect_repo_name_from_path)
        else None
    )

    files = list(iter_files(root))
    total_files = len(files)
    iterator = files
    use_tqdm = False
    try:
        from tqdm import tqdm  # type: ignore

        iterator = tqdm(files, desc="Indexing files", unit="file")
        use_tqdm = True
    except ImportError:
        pass

    log_progress = os.environ.get("INDEX_PROGRESS_LOG", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if log_progress:
        print(f"[index] Found {total_files} files to process under {root}")

    # Track progress in workspace state for live UI updates
    workspace_path = str(root)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        set_indexing_started(workspace_path, total_files)
    except Exception as e:
        logger.debug(f"Failed to set indexing started: {e}")

    # Parallel file processing configuration
    # INDEX_WORKERS=0 or 1 means sequential (default for safety)
    # INDEX_WORKERS=N uses N threads (recommended: 4-8 for I/O-bound indexing)
    # INDEX_WORKERS=-1 uses CPU count
    try:
        index_workers = int(os.environ.get("INDEX_WORKERS", "1") or "1")
    except (ValueError, TypeError):
        index_workers = 1
    if index_workers == -1:
        index_workers = multiprocessing.cpu_count()
    # Cap at reasonable max to avoid overwhelming Qdrant
    max_workers = min(index_workers, 16) if index_workers > 1 else 1

    def _index_file_task(file_path: Path) -> tuple[Path, Optional[Exception], bool]:
        """Task for parallel file indexing.

        Returns:
            (path, error_or_none, was_skipped_due_to_lock)
        """
        per_file_repo = (
            root_repo_for_cache
            if root_repo_for_cache is not None
            else (
                _detect_repo_name_from_path(file_path)
                if _detect_repo_name_from_path
                else None
            )
        )
        try:
            index_single_file(
                client, model, collection, vector_name, file_path,
                dedupe=dedupe, skip_unchanged=skip_unchanged,
                pseudo_mode=pseudo_mode,
                repo_name_for_cache=per_file_repo,
                allowed_vectors=allowed_vectors,
                allowed_sparse=allowed_sparse,
            )
            return (file_path, None, False)
        except LockContendedException:
            # Lock contention - mark for retry
            return (file_path, None, True)
        except Exception as e:
            return (file_path, e, False)

    files_processed = 0
    errors = []
    retry_queue: list[Path] = []

    if max_workers > 1:
        # Parallel processing with ThreadPoolExecutor and skip-and-retry for lock contention
        if log_progress:
            print(f"[index] Using {max_workers} parallel workers")

        # Enable skip-on-contention mode for parallel phase
        # This makes Redis locks fail fast instead of blocking, allowing other files to proceed
        with enable_lock_skip_on_contention():
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_index_file_task, fp): fp for fp in files}
                last_progress_update = 0
                for future in as_completed(futures):
                    files_processed += 1
                    file_path, error, was_skipped = future.result()
                    if was_skipped:
                        # Lock contention - queue for retry
                        retry_queue.append(file_path)
                    elif error:
                        errors.append((file_path, error))
                        print(f"Error indexing {file_path}: {error}")
                    if log_progress and (files_processed % 25 == 0 or files_processed == total_files):
                        print(f"[index] {files_processed}/{total_files} files processed")
                    # Update progress in workspace state every 10 files for live UI
                    if files_processed - last_progress_update >= 10 or files_processed == total_files:
                        last_progress_update = files_processed
                        try:
                            set_indexing_progress(
                                workspace_path, started_at, files_processed, total_files,
                                str(file_path) if file_path else None
                            )
                        except Exception as e:
                            logger.debug(f"Failed to update indexing progress: {e}")

        # Retry phase: process skipped files sequentially (no lock contention mode)
        # This runs OUTSIDE the enable_lock_skip_on_contention context, so locks wait normally
        if retry_queue:
            if log_progress:
                print(f"[index] Retrying {len(retry_queue)} files skipped due to lock contention")
            for file_path in retry_queue:
                _, error, was_skipped = _index_file_task(file_path)
                if error:
                    errors.append((file_path, error))
                    print(f"Error indexing {file_path}: {error}")
                # If still skipped on retry, that's a real issue - count as error
                if was_skipped:
                    errors.append((file_path, LockContendedException("Lock still contended after retry")))
                    logger.warning(f"Lock still contended after retry for {file_path}")
    else:
        # Sequential processing (original behavior) - no retry needed
        last_progress_update = 0
        for file_path in iterator:
            files_processed += 1
            file_path, error, _ = _index_file_task(file_path)
            if error:
                errors.append((file_path, error))
                print(f"Error indexing {file_path}: {error}")
            if log_progress and (files_processed % 25 == 0 or files_processed == total_files):
                print(f"[index] {files_processed}/{total_files} files processed")
            # Update progress in workspace state every 10 files for live UI
            if files_processed - last_progress_update >= 10 or files_processed == total_files:
                last_progress_update = files_processed
                try:
                    set_indexing_progress(
                        workspace_path, started_at, files_processed, total_files,
                        str(file_path) if file_path else None
                    )
                except Exception as e:
                    logger.debug(f"Failed to update indexing progress: {e}")

    if errors and log_progress:
        print(f"[index] Completed with {len(errors)} errors out of {total_files} files")


def process_file_with_smart_reindexing(
    file_path,
    text: str,
    language: str,
    client: QdrantClient,
    current_collection: str,
    per_file_repo,
    model,
    vector_name: str | None,
    *,
    allowed_vectors: set[str] | None = None,
    allowed_sparse: set[str] | None = None,
) -> str:
    """Smart, chunk-level reindexing for a single file.

    Rebuilds all points for the file with *accurate* line numbers while:
    - Reusing existing embeddings/lexical vectors for unchanged chunks (by code content), and
    - Re-embedding only for changed chunks.
    """
    # Allow test monkeypatching on ingest_code.* to be honored here.
    # Must be done FIRST before any helper calls.
    _ingest_mod = None
    try:
        import importlib
        _ingest_mod = importlib.import_module("scripts.ingest_code")
    except Exception:
        _ingest_mod = None
    _embed_batch = getattr(_ingest_mod, "embed_batch", embed_batch) if _ingest_mod else embed_batch
    _upsert_points_fn = getattr(_ingest_mod, "upsert_points", upsert_points) if _ingest_mod else upsert_points
    _delete_points_fn = getattr(_ingest_mod, "delete_points_by_path", delete_points_by_path) if _ingest_mod else delete_points_by_path
    _should_process_pseudo = getattr(_ingest_mod, "should_process_pseudo_for_chunk", should_process_pseudo_for_chunk) if _ingest_mod else should_process_pseudo_for_chunk

    try:
        p = Path(str(file_path))
        if _should_skip_explicit_file_by_excluder(p):
            try:
                _delete_points_fn(client, current_collection, str(p))
            except Exception as e:
                logger.debug(f"Suppressed exception: {e}")
            print(f"[SMART_REINDEX] Skipping excluded file: {file_path}")
            return "skipped"
    except Exception:
        return "skipped"

    print(f"[SMART_REINDEX] Processing {file_path} with chunk-level reindexing")

    try:
        fp = str(file_path)
    except Exception:
        fp = str(file_path)
    try:
        if not isinstance(file_path, Path):
            file_path = Path(fp)
    except Exception:
        file_path = Path(fp)

    file_hash = xxhash.xxh64(text.encode("utf-8", errors="ignore")).hexdigest()

    # FAST PATH: Check if file hash is unchanged - skip entire processing if so
    # This avoids AST parsing for unchanged files (P1 optimization)
    if get_cached_file_hash:
        try:
            cached_file_hash = get_cached_file_hash(fp, per_file_repo)
            if cached_file_hash and cached_file_hash == file_hash:
                print(f"[SMART_REINDEX] {file_path}: file hash unchanged, skipping (fast path)")
                return "skipped"
        except Exception as e:
            logger.debug(f"Suppressed exception: {e}")  # Fall through to normal processing

    if allowed_vectors is None and allowed_sparse is None:
        allowed_vectors, allowed_sparse = get_collection_vector_names(client, current_collection)

    allow_lex = allowed_vectors is None or LEX_VECTOR_NAME in allowed_vectors
    allow_mini = allowed_vectors is None or MINI_VECTOR_NAME in allowed_vectors
    allow_pattern = allowed_vectors is None or PATTERN_VECTOR_NAME in allowed_vectors
    allow_sparse = allowed_sparse is None or LEX_SPARSE_NAME in allowed_sparse
    # Multi-granular vector support: only enable if collection actually has these vectors
    allow_entity = allowed_vectors is None or ENTITY_DENSE_NAME in allowed_vectors
    allow_relation = allowed_vectors is None or RELATION_DENSE_NAME in allowed_vectors

    pattern_vectors_on = os.environ.get("PATTERN_VECTORS", "").strip().lower() in {"1", "true", "yes", "on"}
    pattern_vectors_on = pattern_vectors_on and allow_pattern
    refrag_on = os.environ.get("REFRAG_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    use_mini = refrag_on and allow_mini
    use_sparse = LEX_SPARSE_MODE and allow_sparse
    use_multi_granular = MULTI_GRANULAR_VECTORS and allow_entity and allow_relation

    repo_id: str | None = None
    repo_rel_path: str | None = None
    try:
        repo_tag = per_file_repo
        if logical_repo_reuse_enabled() and get_workspace_state is not None:
            ws_root = os.environ.get("WATCH_ROOT") or os.environ.get("WORKSPACE_PATH") or "/work"
            state = get_workspace_state(ws_root, repo_tag)
            lrid = state.get("logical_repo_id") if isinstance(state, dict) else None
            if isinstance(lrid, str) and lrid.strip():
                repo_id = lrid.strip()
            try:
                ws_base = Path(ws_root).resolve()
                repo_root = ws_base
                if repo_tag:
                    candidate = ws_base / str(repo_tag)
                    if candidate.exists():
                        repo_root = candidate
                rel = file_path.resolve().relative_to(repo_root)
                repo_rel_path = rel.as_posix()
            except Exception:
                repo_rel_path = None
    except Exception as e:
        print(f"[SMART_REINDEX] Failed to derive logical repo identity for {file_path}: {e}")

    symbol_meta = extract_symbols_with_tree_sitter(fp, content=text, language=language)
    if not symbol_meta:
        print(f"[SMART_REINDEX] No symbols found in {file_path}, falling back to full reindex")
        return "failed"

    cached_symbols = get_cached_symbols(fp) if get_cached_symbols else {}
    unchanged_symbols: list[str] = []
    changed_symbols: list[str] = []
    if cached_symbols and compare_symbol_changes:
        try:
            unchanged_symbols, changed_symbols = compare_symbol_changes(
                cached_symbols, symbol_meta
            )
        except Exception:
            unchanged_symbols = []
            changed_symbols = list(symbol_meta.keys())
    else:
        changed_symbols = list(symbol_meta.keys())
    changed_set = set(changed_symbols)

    if len(changed_symbols) == 0 and cached_symbols:
        print(f"[SMART_REINDEX] {file_path}: 0 changes detected, skipping")
        return "skipped"

    existing_points = []
    try:
        filt = models.Filter(
            must=[
                models.FieldCondition(
                    key="metadata.path", match=models.MatchValue(value=fp)
                )
            ]
        )
        next_offset = None
        while True:
            pts, next_offset = client.scroll(
                collection_name=current_collection,
                scroll_filter=filt,
                with_payload=True,
                with_vectors=True,
                limit=256,
                offset=next_offset,
            )
            if not pts:
                break
            existing_points.extend(pts)
            if next_offset is None:
                break
    except Exception as e:
        print(f"[SMART_REINDEX] Failed to load existing points for {file_path}: {e}")
        existing_points = []

    points_by_code: dict[tuple[str, str, str], list] = {}
    try:
        for rec in existing_points:
            payload = rec.payload or {}
            md = payload.get("metadata") or {}
            code_text = md.get("code") or ""
            # Determine the embedding input used for the existing point.
            # - Legacy: dense_text existed only for text-like files.
            # - Current: dense_mode records whether we embed code or info.
            embed_text = payload.get("dense_text")
            if not embed_text:
                dense_mode = payload.get("dense_mode")
                if dense_mode:
                    embed_text = _select_dense_text(
                        info=payload.get("information") or payload.get("document") or "",
                        code_text=code_text,
                        pseudo=payload.get("pseudo") or "",
                        tags=payload.get("tags") if isinstance(payload.get("tags"), list) else None,
                        mode=str(dense_mode),
                    )
                else:
                    embed_text = payload.get("information") or payload.get("document") or ""
            kind = md.get("kind") or ""
            sym_name = md.get("symbol") or ""
            start_line = md.get("start_line") or 0
            symbol_id = (
                f"{kind}_{sym_name}_{start_line}"
                if kind and sym_name and start_line
                else ""
            )
            key = (symbol_id, code_text, embed_text) if symbol_id else ("", code_text, embed_text)
            points_by_code.setdefault(key, []).append(rec)
    except Exception:
        points_by_code = {}

    CHUNK_LINES = int(os.environ.get("INDEX_CHUNK_LINES", "120") or 120)
    CHUNK_OVERLAP = int(os.environ.get("INDEX_CHUNK_OVERLAP", "20") or 20)
    use_micro = os.environ.get("INDEX_MICRO_CHUNKS", "0").lower() in {"1", "true", "yes", "on"}
    use_semantic = os.environ.get("INDEX_SEMANTIC_CHUNKS", "1").lower() in {"1", "true", "yes", "on"}

    if use_micro:
        try:
            _cap = int(os.environ.get("MAX_MICRO_CHUNKS_PER_FILE", "200") or 200)
            _base_tokens = int(os.environ.get("MICRO_CHUNK_TOKENS", "128") or 128)
            _base_stride = int(os.environ.get("MICRO_CHUNK_STRIDE", "64") or 64)
            chunks = chunk_by_tokens(text, k_tokens=_base_tokens, stride_tokens=_base_stride)
            if _cap > 0 and len(chunks) > _cap:
                _before = len(chunks)
                _scale = (len(chunks) / _cap) * 1.1
                _new_tokens = max(_base_tokens, int(_base_tokens * _scale))
                _new_stride = max(_base_stride, int(_base_stride * _scale))
                chunks = chunk_by_tokens(text, k_tokens=_new_tokens, stride_tokens=_new_stride)
                try:
                    print(
                        f"[SMART_REINDEX] micro-chunks resized path={file_path} count={_before}->{len(chunks)} "
                        f"tokens={_base_tokens}->{_new_tokens} stride={_base_stride}->{_new_stride}"
                    )
                except Exception as e:
                    logger.debug(f"Suppressed exception: {e}")
        except Exception:
            chunks = chunk_by_tokens(text)
    elif use_semantic:
        chunks = chunk_semantic(text, language, CHUNK_LINES, CHUNK_OVERLAP)
    else:
        chunks = chunk_lines(text, CHUNK_LINES, CHUNK_OVERLAP)

    is_text_like = _is_text_like_language(language)
    ast_info = _ast_analyze_file(file_path, language, text)
    symbol_spans = ast_info.get("symbol_spans") or _extract_symbols(language, text)
    symbol_meta_by_path = ast_info.get("symbol_meta_by_path", {})
    symbol_meta_by_name = ast_info.get("symbol_meta_by_name", {})
    ast_call_refs = ast_info.get("call_refs", [])
    ast_import_refs = ast_info.get("import_refs", [])
    symbol_calls = ast_info.get("symbol_calls", {})

    reused_points: list[models.PointStruct] = []
    embed_texts: list[str] = []
    embed_payloads: list[dict] = []
    embed_ids: list[int] = []
    embed_lex: list[list[float]] = []
    embed_lex_text: list[str] = []
    embed_code: list[str] = []  # Raw code for pattern vectors
    # Multi-granular vector batches
    embed_entity_texts: list[str] = []  # For entity_dense vectors
    embed_relation_texts: list[str] = []  # For relation_dense vectors

    imports = ast_info.get("imports")
    calls = ast_info.get("calls")
    # Always get import_map for callee resolution (ast_info doesn't provide it)
    _, _, import_map = _get_imports_calls(language, text)
    # Get class inheritance relationships
    inheritance_map = _get_inheritance(language, text)
    if "imports" not in ast_info or "calls" not in ast_info:
        base_imports, base_calls, _ = _get_imports_calls(language, text)
        if "imports" not in ast_info:
            imports = base_imports
        if "calls" not in ast_info:
            calls = base_calls
    last_mod, churn_count, author_count = _git_metadata(file_path)

    pseudo_batch_concurrency = int(os.environ.get("PSEUDO_BATCH_CONCURRENCY", "1") or 1)
    use_batch_pseudo = pseudo_batch_concurrency > 1

    chunk_data_sr: list[dict] = []
    # Mapping from symbol_path to point_id for graph edge extraction
    symbol_path_to_point_id_sr: dict[str, str] = {}
    for ch in chunks:
        info = build_information(
            language, file_path, ch["start"], ch["end"],
            ch["text"].splitlines()[0] if ch["text"] else "",
        )
        kind, sym, sym_path = _choose_symbol_for_chunk(ch["start"], ch["end"], symbol_spans)
        if "kind" in ch and ch.get("kind"):
            kind = ch.get("kind") or kind
        if "symbol" in ch and ch.get("symbol"):
            sym = ch.get("symbol") or sym
        if "symbol_path" in ch and ch.get("symbol_path"):
            sym_path = ch.get("symbol_path") or sym_path
        if not ch.get("kind") and kind:
            ch["kind"] = kind
        if not ch.get("symbol") and sym:
            ch["symbol"] = sym
        if not ch.get("symbol_path") and sym_path:
            ch["symbol_path"] = sym_path

        # Slice chunk-specific calls/imports from AST refs
        if not ch.get("calls") and ast_call_refs:
            ch["calls"] = _slice_calls_for_range(ast_call_refs, ch["start"], ch["end"])
        if not ch.get("imports") and ast_import_refs:
            ch["imports"] = _slice_imports_for_range(ast_import_refs, ch["start"], ch["end"])

        # Enrich chunk with symbol metadata
        symbol_info = None
        if sym_path:
            symbol_info = symbol_meta_by_path.get(sym_path)
        if symbol_info is None and sym:
            symbol_info = symbol_meta_by_name.get(sym)
        if symbol_info is not None:
            ch["symbol_start_line"] = int(getattr(symbol_info, "start_line", 0) or 0)
            ch["symbol_end_line"] = int(getattr(symbol_info, "end_line", 0) or 0)
            signature = str(getattr(symbol_info, "signature", "") or "")
            docstring = str(getattr(symbol_info, "docstring", "") or "")
            parent = str(getattr(symbol_info, "parent", "") or "")
            if signature:
                ch["symbol_signature"] = signature
            if docstring:
                ch["symbol_docstring"] = docstring
            if parent:
                ch["symbol_parent"] = parent

        _cur_path = str(file_path)
        _host_path, _container_path = _compute_host_and_container_paths(_cur_path)

        payload = {
            "document": info,
            "information": info,
            "metadata": {
                "path": str(file_path),
                "path_prefix": str(file_path.parent),
                "ext": str(file_path.suffix).lstrip(".").lower(),
                "language": language,
                "kind": kind,
                "symbol": sym,
                "symbol_path": sym_path or "",
                "repo": per_file_repo,
                "start_line": ch["start"],
                "end_line": ch["end"],
                "code": ch["text"],
                "file_hash": file_hash,
                # Use chunk-specific calls/imports when available (semantic chunks),
                # otherwise fall back to file-level calls/imports
                "imports": ch.get("imports") if ch.get("imports") else imports,
                "calls": ch.get("calls") if ch.get("calls") else calls,
                # Import map for callee resolution: local_name -> qualified_path
                "import_map": import_map if import_map else None,
                # Inheritance map for class hierarchy: class_name -> [base_classes]
                "inheritance_map": inheritance_map if inheritance_map else None,
                "symbol_start_line": ch.get("symbol_start_line"),
                "symbol_end_line": ch.get("symbol_end_line"),
                "symbol_signature": ch.get("symbol_signature"),
                "symbol_docstring": ch.get("symbol_docstring"),
                "symbol_parent": ch.get("symbol_parent"),
                "ingested_at": int(time.time()),
                "last_modified_at": int(last_mod),
                "churn_count": int(churn_count),
                "author_count": int(author_count),
                "host_path": _host_path,
                "container_path": _container_path,
                "repo_id": repo_id,
                "repo_rel_path": repo_rel_path,
            },
        }

        needs_pseudo_gen, cached_pseudo, cached_tags = _should_process_pseudo(
            fp, ch, changed_set
        )

        chunk_data_sr.append({
            "chunk": ch,
            "info": info,
            "payload": payload,
            "kind": kind,
            "sym": sym,
            "sym_path": sym_path,
            "needs_pseudo": needs_pseudo_gen,
            "cached_pseudo": cached_pseudo,
            "cached_tags": cached_tags,
        })

    if use_batch_pseudo:
        pending_indices = [i for i, cd in enumerate(chunk_data_sr) if cd["needs_pseudo"]]
        pending_texts = [chunk_data_sr[i]["chunk"].get("text") or "" for i in pending_indices]

        if pending_texts:
            try:
                from scripts.refrag_glm import generate_pseudo_tags_batch
                batch_results = generate_pseudo_tags_batch(pending_texts, concurrency=pseudo_batch_concurrency)
                for idx, (pseudo, tags) in zip(pending_indices, batch_results):
                    chunk_data_sr[idx]["cached_pseudo"] = pseudo
                    chunk_data_sr[idx]["cached_tags"] = tags
                    chunk_data_sr[idx]["needs_pseudo"] = False
                    if pseudo or tags:
                        ch = chunk_data_sr[idx]["chunk"]
                        symbol_name = ch.get("symbol", "")
                        if symbol_name and set_cached_pseudo:
                            k = ch.get("kind", "unknown")
                            start_line = ch.get("start", 0)
                            sid = f"{k}_{symbol_name}_{start_line}"
                            set_cached_pseudo(fp, sid, pseudo, tags, file_hash)
            except Exception as e:
                print(f"[PSEUDO_BATCH] Smart reindex batch failed, falling back: {e}")
                use_batch_pseudo = False

    for cd in chunk_data_sr:
        ch = cd["chunk"]
        payload = cd["payload"]
        pseudo = cd["cached_pseudo"]
        tags = cd["cached_tags"]

        if not use_batch_pseudo and cd["needs_pseudo"]:
            try:
                pseudo, tags = generate_pseudo_tags(ch.get("text") or "")
                if pseudo or tags:
                    symbol_name = ch.get("symbol", "")
                    if symbol_name:
                        k = ch.get("kind", "unknown")
                        start_line = ch.get("start", 0)
                        sid = f"{k}_{symbol_name}_{start_line}"
                        if set_cached_pseudo:
                            set_cached_pseudo(fp, sid, pseudo, tags, file_hash)
            except Exception as e:
                logger.debug(f"Suppressed exception: {e}")

        if pseudo:
            payload["pseudo"] = pseudo
        if tags:
            payload["tags"] = tags

        info = cd["info"]
        kind = cd["kind"]
        sym = cd["sym"]
        sym_path = cd.get("sym_path") or ""

        code_text = ch.get("text") or ""
        # Compute point ID early for symbol_path mapping
        chunk_point_id = hash_id(code_text, fp, ch["start"], ch["end"])
        if sym_path:
            symbol_path_to_point_id_sr[sym_path] = str(chunk_point_id)
        dense_mode = (
            str(os.environ.get("INDEX_DENSE_MODE", "info+pseudo+tags") or "")
            .strip()
            .lower()
            or "info+pseudo+tags"
        )
        payload["dense_mode"] = dense_mode
        dense_text = _select_dense_text(
            info=info,
            code_text=code_text,
            pseudo=pseudo,
            tags=tags,
            mode=dense_mode,
        )
        chunk_symbol_id = ""
        if sym and kind:
            chunk_symbol_id = f"{kind}_{sym}_{ch['start']}"

        reuse_key = (chunk_symbol_id, code_text, dense_text)
        fallback_key = ("", code_text, dense_text)
        reused_rec = None
        used_key = None
        bucket = points_by_code.get(reuse_key)
        if bucket is not None:
            used_key = reuse_key
        else:
            bucket = points_by_code.get(fallback_key)
            if bucket is not None:
                used_key = fallback_key
        if bucket:
            try:
                reused_rec = bucket.pop()
                if not bucket:
                    if used_key is not None:
                        points_by_code.pop(used_key, None)
            except Exception:
                reused_rec = None

        if reused_rec is not None:
            try:
                vec = reused_rec.vector
                if vector_name and isinstance(vec, dict) and vector_name not in vec:
                    raise ValueError("reused vector missing dense key")
                aug_lex_text = (code_text or "") + (" " + pseudo if pseudo else "") + (
                    " " + " ".join(tags) if tags else ""
                )
                refreshed_lex = _lex_hash_vector_text(aug_lex_text)
                if vector_name:
                    if isinstance(vec, dict):
                        vec = dict(vec)
                        if allow_lex:
                            vec[LEX_VECTOR_NAME] = refreshed_lex
                        if use_sparse and aug_lex_text:
                            try:
                                sparse_vec = _lex_sparse_vector_text(aug_lex_text)
                                if sparse_vec.get("indices"):
                                    vec[LEX_SPARSE_NAME] = models.SparseVector(**sparse_vec)
                            except Exception as e:
                                logger.debug(f"Suppressed exception: {e}")
                    else:
                        vecs = {vector_name: vec}
                        if allow_lex:
                            vecs[LEX_VECTOR_NAME] = refreshed_lex
                        try:
                            if use_mini:
                                vecs[MINI_VECTOR_NAME] = project_mini(list(vec), MINI_VEC_DIM)
                        except Exception as e:
                            logger.debug(f"Suppressed exception: {e}")
                        if use_sparse and aug_lex_text:
                            try:
                                sparse_vec = _lex_sparse_vector_text(aug_lex_text)
                                if sparse_vec.get("indices"):
                                    vecs[LEX_SPARSE_NAME] = models.SparseVector(**sparse_vec)
                            except Exception as e:
                                logger.debug(f"Suppressed exception: {e}")
                        vec = vecs
                else:
                    if isinstance(vec, dict):
                        dense = None
                        try:
                            for k, v in vec.items():
                                if k not in {LEX_VECTOR_NAME, MINI_VECTOR_NAME}:
                                    dense = v
                                    break
                        except Exception:
                            dense = None
                        if dense is None:
                            raise ValueError("reused vector has no dense component")
                        vec = dense
                reused_points.append(
                    models.PointStruct(id=chunk_point_id, vector=vec, payload=payload)
                )
                continue
            except Exception as e:
                logger.debug(f"Suppressed exception: {e}")

        embed_texts.append(dense_text)
        embed_payloads.append(payload)
        embed_ids.append(chunk_point_id)
        aug_lex_text = (code_text or "") + (" " + pseudo if pseudo else "") + (" " + " ".join(tags) if tags else "")
        embed_lex.append(_lex_hash_vector_text(aug_lex_text))
        embed_lex_text.append(aug_lex_text)
        embed_code.append(code_text or "")

        # Generate multi-granular vector texts
        if use_multi_granular:
            entity_text = _select_entity_text(
                symbol=ch.get("symbol") or sym or "",
                symbol_path=ch.get("symbol_path") or sym_path or "",
                signature=ch.get("symbol_signature") or "",
                docstring=ch.get("symbol_docstring") or "",
                parent=ch.get("symbol_parent") or "",
            )
            relation_text = _select_relation_text(
                calls=ch.get("calls") if ch.get("calls") else calls,
                imports=ch.get("imports") if ch.get("imports") else imports,
            )
            embed_entity_texts.append(entity_text)
            embed_relation_texts.append(relation_text)
        else:
            embed_entity_texts.append("")
            embed_relation_texts.append("")

    new_points: list[models.PointStruct] = []
    if embed_texts:
        vectors = _embed_batch(model, embed_texts)

        # Embed multi-granular vectors if enabled
        entity_vectors: list[list] = []
        relation_vectors: list[list] = []
        if use_multi_granular and embed_entity_texts:
            # Filter non-empty texts to embed, track indices
            entity_to_embed = [(i, t) for i, t in enumerate(embed_entity_texts) if t.strip()]
            relation_to_embed = [(i, t) for i, t in enumerate(embed_relation_texts) if t.strip()]

            # Initialize with empty lists
            entity_vectors = [[] for _ in embed_entity_texts]
            relation_vectors = [[] for _ in embed_relation_texts]

            # Batch embed entity texts
            if entity_to_embed:
                try:
                    ent_indices, ent_texts = zip(*entity_to_embed)
                    ent_vecs = _embed_batch(model, list(ent_texts))
                    for idx, evec in zip(ent_indices, ent_vecs):
                        entity_vectors[idx] = evec
                except Exception as e:
                    logger.warning(f"Failed to embed {len(entity_to_embed)} entity texts: {e}")

            # Batch embed relation texts
            if relation_to_embed:
                try:
                    rel_indices, rel_texts = zip(*relation_to_embed)
                    rel_vecs = _embed_batch(model, list(rel_texts))
                    for idx, rvec in zip(rel_indices, rel_vecs):
                        relation_vectors[idx] = rvec
                except Exception as e:
                    logger.warning(f"Failed to embed {len(relation_to_embed)} relation texts: {e}")
        else:
            entity_vectors = [[] for _ in embed_texts]
            relation_vectors = [[] for _ in embed_texts]

        for pid, v, lx, pl, lt, ct, ev, rv in zip(
            embed_ids, vectors, embed_lex, embed_payloads, embed_lex_text, embed_code,
            entity_vectors, relation_vectors
        ):
            if vector_name:
                vecs = {vector_name: v}
                if allow_lex:
                    vecs[LEX_VECTOR_NAME] = lx
                try:
                    if use_mini:
                        vecs[MINI_VECTOR_NAME] = project_mini(list(v), MINI_VEC_DIM)
                except Exception as e:
                    logger.debug(f"Suppressed exception: {e}")
                # Add pattern vector for structural similarity search
                if pattern_vectors_on and ct:
                    try:
                        pv = extract_pattern_vector(ct, language)
                        if pv:
                            vecs[PATTERN_VECTOR_NAME] = pv
                    except Exception as e:
                        logger.debug(f"Suppressed exception: {e}")
                if use_sparse and lt:
                    sparse_vec = _lex_sparse_vector_text(lt)
                    if sparse_vec.get("indices"):
                        vecs[LEX_SPARSE_NAME] = models.SparseVector(**sparse_vec)
                # Add multi-granular vectors
                if use_multi_granular:
                    if ev:
                        vecs[ENTITY_DENSE_NAME] = ev
                    if rv:
                        vecs[RELATION_DENSE_NAME] = rv
                new_points.append(models.PointStruct(id=pid, vector=vecs, payload=pl))
            else:
                new_points.append(models.PointStruct(id=pid, vector=v, payload=pl))

    all_points = reused_points + new_points

    try:
        _delete_points_fn(client, current_collection, fp)
    except Exception as e:
        print(f"[SMART_REINDEX] Failed to delete old points for {file_path}: {e}")

    if all_points:
        _upsert_points_fn(client, current_collection, all_points)

        # Emit graph edges for symbol relationships (always on - Qdrant flat graph)
        # Neo4j takes precedence when NEO4J_GRAPH=1 is set
        # Always try symbol-level edges first, fall back to file-level if no symbol_calls
        try:
            graph_coll = ensure_graph_collection(client, current_collection)
            delete_edges_by_path(client, graph_coll, fp, repo=per_file_repo)

            all_edges = []
            if symbol_calls:
                # Symbol-level edges: use AST-extracted caller→callee relationships
                for caller, callees in symbol_calls.items():
                    if not caller or not callees:
                        continue
                    start_line = None
                    sym_info = symbol_meta_by_path.get(caller) or symbol_meta_by_name.get(caller)
                    if sym_info is not None:
                        try:
                            start_line = int(getattr(sym_info, "start_line", 0) or 0)
                        except Exception:
                            start_line = None
                    # Get caller_point_id from symbol_path mapping
                    caller_pid = symbol_path_to_point_id_sr.get(caller)
                    all_edges.extend(
                        _extract_call_edges_compat(
                            symbol_path=caller,
                            calls=callees,
                            path=fp,
                            repo=per_file_repo,
                            start_line=start_line,
                            language=language,
                            caller_point_id=caller_pid,
                            import_paths=import_map,
                        )
                    )
                if imports:
                    # For file-level imports, use first point ID if available
                    file_pid = next(iter(symbol_path_to_point_id_sr.values()), None) if symbol_path_to_point_id_sr else None
                    all_edges.extend(
                        _extract_import_edges_compat(
                            symbol_path=fp,
                            imports=imports,
                            path=fp,
                            repo=per_file_repo,
                            language=language,
                            caller_point_id=file_pid,
                        )
                    )
            else:
                # File-level fallback: emit file→symbol edges
                meta0 = {}
                try:
                    if all_points and hasattr(all_points[0], "payload"):
                        meta0 = all_points[0].payload.get("metadata", {}) or {}
                except Exception:
                    meta0 = {}
                file_calls = meta0.get("calls", []) or []
                file_imports = meta0.get("imports", []) or []
                file_import_map = meta0.get("import_map", {}) or {}
                # Use first point ID for file-level edges
                file_pid = next(iter(symbol_path_to_point_id_sr.values()), None) if symbol_path_to_point_id_sr else None
                if file_calls:
                    all_edges.extend(_extract_call_edges_compat(
                        symbol_path=fp,
                        calls=file_calls,
                        path=fp,
                        repo=per_file_repo,
                        caller_point_id=file_pid,
                        import_paths=file_import_map,
                    ))
                if file_imports:
                    all_edges.extend(_extract_import_edges_compat(
                        symbol_path=fp,
                        imports=file_imports,
                        path=fp,
                        repo=per_file_repo,
                        caller_point_id=file_pid,
                    ))

            # Extract inheritance edges (INHERITS_FROM) for all classes
            if inheritance_map:
                for class_name, base_classes in inheritance_map.items():
                    if class_name and base_classes:
                        all_edges.extend(extract_inheritance_edges(
                            class_name=class_name,
                            base_classes=base_classes,
                            path=fp,
                            repo=per_file_repo,
                            language=language,
                            import_paths=import_map,
                        ))

            if all_edges:
                upsert_edges(client, graph_coll, all_edges)
        except Exception as e:
            logger.warning(f"Failed to emit graph edges for {fp}: {e}")

    try:
        if set_cached_symbols:
            set_cached_symbols(fp, symbol_meta, file_hash)
    except Exception as e:
        logger.warning(f"Failed to update symbol cache for {file_path}: {e}")
    try:
        if set_cached_file_hash:
            set_cached_file_hash(fp, file_hash, per_file_repo)
    except Exception as e:
        logger.debug(f"Suppressed exception: {e}")

    print(
        f"[SMART_REINDEX] Completed {file_path}: chunks={len(chunks)}, reused_points={len(reused_points)}, embedded_points={len(new_points)}"
    )
    return "success"


# Import pseudo_backfill_tick for backward compatibility
def pseudo_backfill_tick(
    client: QdrantClient,
    collection: str,
    repo_name: str | None = None,
    *,
    max_points: int = 256,
    dim: int | None = None,
    vector_name: str | None = None,
    schema_mode: str | None = None,
    allowed_vectors: set[str] | None = None,
    allowed_sparse: set[str] | None = None,
) -> int:
    """Best-effort pseudo/tag backfill for a collection."""
    from scripts.ingest.qdrant import ensure_collection_and_indexes_once
    
    if not collection or max_points <= 0:
        return 0

    try:
        from qdrant_client import models as _models
    except Exception:
        return 0

    must_conditions: list[Any] = []
    if repo_name:
        try:
            must_conditions.append(
                _models.FieldCondition(
                    key="metadata.repo",
                    match=_models.MatchValue(value=repo_name),
                )
            )
        except Exception as e:
            logger.debug(f"Suppressed exception: {e}")

    flt = None
    try:
        null_cond = getattr(_models, "IsNullCondition", None)
        empty_cond = getattr(_models, "IsEmptyCondition", None)
        if null_cond is not None:
            should_conditions = []
            try:
                should_conditions.append(null_cond(is_null="pseudo"))
            except Exception as e:
                logger.debug(f"Suppressed exception: {e}")
            try:
                should_conditions.append(null_cond(is_null="tags"))
            except Exception as e:
                logger.debug(f"Suppressed exception: {e}")
            if empty_cond is not None:
                try:
                    should_conditions.append(empty_cond(is_empty="tags"))
                except Exception as e:
                    logger.debug(f"Suppressed exception: {e}")
            flt = _models.Filter(
                must=must_conditions or None,
                should=should_conditions or None,
            )
        else:
            flt = _models.Filter(must=must_conditions or None)
    except Exception:
        flt = None

    processed = 0
    debug_enabled = (os.environ.get("PSEUDO_BACKFILL_DEBUG") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    next_offset = None

    if allowed_vectors is None and allowed_sparse is None:
        allowed_vectors, allowed_sparse = get_collection_vector_names(client, collection)
    allow_lex = allowed_vectors is None or LEX_VECTOR_NAME in allowed_vectors
    allow_sparse = allowed_sparse is None or LEX_SPARSE_NAME in allowed_sparse
    use_sparse = LEX_SPARSE_MODE and allow_sparse

    def _maybe_ensure_collection() -> bool:
        if not dim or not vector_name:
            return False
        try:
            ensure_collection_and_indexes_once(
                client,
                collection,
                int(dim),
                vector_name,
                schema_mode=schema_mode,
            )
            return True
        except Exception:
            return False

    while processed < max_points:
        batch_limit = max(1, min(64, max_points - processed))
        try:
            points, next_offset = client.scroll(
                collection_name=collection,
                scroll_filter=flt,
                limit=batch_limit,
                with_payload=True,
                with_vectors=True,
                offset=next_offset,
            )
        except Exception:
            if _maybe_ensure_collection():
                try:
                    points, next_offset = client.scroll(
                        collection_name=collection,
                        scroll_filter=flt,
                        limit=batch_limit,
                        with_payload=True,
                        with_vectors=True,
                        offset=next_offset,
                    )
                except Exception:
                    break
            else:
                break

        if not points:
            break

        new_points: list[Any] = []
        for rec in points:
            try:
                payload = rec.payload or {}
                md = payload.get("metadata") or {}
                code = md.get("code") or ""
                if not code:
                    continue

                pseudo = payload.get("pseudo") or ""
                tags_val = payload.get("tags") or []
                tags: list[str] = list(tags_val) if isinstance(tags_val, list) else []

                if not pseudo and not tags:
                    try:
                        pseudo, tags = generate_pseudo_tags(code)
                    except Exception:
                        pseudo, tags = "", []

                if not pseudo and not tags:
                    continue

                payload["pseudo"] = pseudo
                payload["tags"] = tags

                aug_text = f"{code} {pseudo} {' '.join(tags)}".strip()
                lex_vec = _lex_hash_vector_text(aug_text)

                vec = rec.vector
                if isinstance(vec, dict):
                    vecs = dict(vec)
                    if allow_lex:
                        vecs[LEX_VECTOR_NAME] = lex_vec
                    new_vec = vecs
                else:
                    new_vec = vec

                if use_sparse and aug_text and isinstance(new_vec, dict):
                    sparse_vec = _lex_sparse_vector_text(aug_text)
                    if sparse_vec.get("indices"):
                        new_vec[LEX_SPARSE_NAME] = models.SparseVector(**sparse_vec)

                new_points.append(
                    models.PointStruct(
                        id=rec.id,
                        vector=new_vec,
                        payload=payload,
                    )
                )
                processed += 1
            except Exception as e:
                logger.debug(f"Suppressed exception, continuing: {e}")
                continue

        if new_points:
            try:
                upsert_points(client, collection, new_points)
            except Exception as e:
                logger.warning(f"Upsert failed for {len(new_points)} points, retrying after ensure_collection: {e}")
                if _maybe_ensure_collection():
                    try:
                        upsert_points(client, collection, new_points)
                    except Exception as e2:
                        logger.error(f"Upsert retry failed for {len(new_points)} points: {e2}")
                        break
                else:
                    logger.error(f"Upsert failed and collection ensure failed; aborting backfill")
                    break

        if next_offset is None:
            break

    return processed


def graph_backfill_tick(
    client: QdrantClient,
    collection: str,
    repo_name: str | None = None,
    *,
    max_points: int = 256,
) -> int:
    """Backfill graph edges from existing indexed points that have calls/imports metadata.

    This enables seamless graph collection population for legacy collections that were
    indexed before graph edges were introduced. Runs incrementally, processing points
    that have calls/imports but haven't been added to the graph collection yet.

    Args:
        client: Qdrant client
        collection: Main collection name (graph collection is derived)
        repo_name: Optional repo filter
        max_points: Maximum points to process per tick

    Returns:
        Number of points processed
    """
    from qdrant_client import models as _models
    # Use module-level imports for graph_edges functions (already imported at top)

    if not collection or max_points <= 0:
        return 0

    # Ensure graph collection exists
    graph_coll = ensure_graph_collection(client, collection)
    if not graph_coll:
        return 0

    # Track which paths we've already backfilled in this session
    # to avoid re-processing on subsequent ticks
    backfill_marker_key = "metadata._graph_backfilled"

    # Build filter: points with calls OR imports that haven't been backfilled
    must_conditions: list[Any] = []
    if repo_name:
        must_conditions.append(
            _models.FieldCondition(
                key="metadata.repo",
                match=_models.MatchValue(value=repo_name),
            )
        )

    # Note: Qdrant doesn't have a simple "array not empty" filter, so we scroll
    # all points and check calls/imports presence in code.

    # Check if we should skip already-backfilled points
    # Use must_not with MatchValue(True) instead of IsNullCondition
    # IsNullCondition only matches keys that exist AND are null, not missing keys
    must_not_conditions: list[Any] = []
    if os.environ.get("GRAPH_BACKFILL_MARK", "0").lower() in {"1", "true"}:
        try:
            must_not_conditions.append(
                _models.FieldCondition(
                    key=backfill_marker_key,
                    match=_models.MatchValue(value=True),
                )
            )
        except Exception as e:
            logger.debug(f"Suppressed exception: {e}")

    flt = _models.Filter(
        must=must_conditions or None,
        must_not=must_not_conditions or None,
    ) if must_conditions or must_not_conditions else None

    processed = 0
    next_offset = None
    edges_created = 0
    paths_cleaned: set[str] = set()  # Track paths where we've deleted old edges

    while processed < max_points:
        batch_limit = min(64, max_points - processed)
        try:
            points, next_offset = client.scroll(
                collection_name=collection,
                scroll_filter=flt,
                limit=batch_limit,
                with_payload=True,
                with_vectors=False,
                offset=next_offset,
            )
        except Exception as e:
            print(f"[graph_backfill] Scroll error: {e}")
            break

        if not points:
            break

        all_edges: list[dict] = []
        points_to_mark: list[Any] = []

        for pt in points:
            try:
                payload = pt.payload or {}
                md = payload.get("metadata", {})

                path = md.get("path") or md.get("file_path") or ""
                if not path:
                    continue

                calls = md.get("calls") or []
                imports = md.get("imports") or []
                import_map = md.get("import_map") or {}
                inheritance_map = md.get("inheritance_map") or {}

                repo = md.get("repo") or repo_name or ""
                language = md.get("language")
                symbol_path = md.get("symbol_path")  # No fallback to path - only use true symbol identifiers

                # NOTE: During backfill, we do NOT delete edges - we only upsert.
                # Deletion would cause data loss since paths_cleaned resets on each call
                # and multiple chunks can exist per path.
                # Edge upsert uses MERGE on edge_id which handles updates correctly.
                paths_cleaned.add(path)  # Track for logging only

                # Skip if no relationship data (but still mark as processed)
                if not calls and not imports and not inheritance_map:
                    pass  # Still mark point below
                else:
                    # Extract edges - only if we have a true symbol identifier
                    # Use point ID as caller_point_id for graph edge linking
                    caller_pid = str(pt.id) if pt.id is not None else None
                    if symbol_path:
                        if calls:
                            all_edges.extend(_extract_call_edges_compat(
                                symbol_path=symbol_path,
                                calls=calls,
                                path=path,
                                repo=repo,
                                language=language,
                                caller_point_id=caller_pid,
                                import_paths=import_map,
                            ))

                        if imports:
                            all_edges.extend(_extract_import_edges_compat(
                                symbol_path=symbol_path,
                                imports=imports,
                                path=path,
                                repo=repo,
                                language=language,
                                caller_point_id=caller_pid,
                            ))

                    # Extract inheritance edges (INHERITS_FROM) for all classes
                    if inheritance_map:
                        for class_name, base_classes in inheritance_map.items():
                            if class_name and base_classes:
                                all_edges.extend(extract_inheritance_edges(
                                    class_name=class_name,
                                    base_classes=base_classes,
                                    path=path,
                                    repo=repo,
                                    language=language,
                                    import_paths=import_map,
                                ))
                points_to_mark.append(pt)
                processed += 1

            except Exception as e:
                print(f"[graph_backfill] Point processing error: {e}")
                continue

        # Upsert edges in batch - use dynamic backend detection
        if all_edges:
            try:
                # Check if Neo4j is enabled at runtime (not just import time)
                if is_neo4j_enabled():
                    from scripts.graph_backends.base import GraphEdge
                    from scripts.graph_backends.ingest_adapter import upsert_edges as neo4j_upsert
                    graph_edges = []
                    for e in all_edges:
                        payload = e.get("payload", {})
                        graph_edges.append(GraphEdge(
                            id=e.get("id", ""),
                            caller_symbol=payload.get("caller_symbol", ""),
                            callee_symbol=payload.get("callee_symbol", ""),
                            caller_path=payload.get("caller_path", ""),
                            callee_path=payload.get("callee_path"),
                            edge_type=payload.get("edge_type", ""),
                            repo=payload.get("repo", ""),
                            start_line=payload.get("start_line"),
                            end_line=payload.get("end_line"),
                            language=payload.get("language"),
                            caller_point_id=payload.get("caller_point_id"),
                        ))
                    count = neo4j_upsert(client, graph_coll, graph_edges)
                else:
                    count = upsert_edges(client, graph_coll, all_edges)
                edges_created += count
            except Exception as e:
                print(f"[graph_backfill] Edge upsert error: {e}")

        # Mark points as backfilled (optional - adds a marker to prevent re-processing)
        # This is a lightweight update that just sets a flag
        if points_to_mark and os.environ.get("GRAPH_BACKFILL_MARK", "0").lower() in {"1", "true"}:
            for pt in points_to_mark:
                try:
                    client.set_payload(
                        collection_name=collection,
                        payload={"metadata": {"_graph_backfilled": True}},
                        points=[pt.id],
                    )
                except Exception as e:
                    logger.debug(f"Suppressed exception: {e}")

        if next_offset is None:
            break

    if processed > 0:
        print(f"[graph_backfill] Processed {processed} points, created {edges_created} edges for {len(paths_cleaned)} paths")

    return processed
