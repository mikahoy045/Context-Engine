#!/usr/bin/env python3
# Copyright 2025 John Donalson and Context-Engine Contributors.
# Licensed under the Business Source License 1.1.
# See the LICENSE file in the repository root for full terms.
"""
Minimal MCP (SSE) companion server exposing:
- qdrant-list: list collections
- qdrant-index: index the currently mounted path (/work or /work/<subdir>)
- qdrant-prune: prune stale points for the mounted path

This server is designed to run in a Docker container with the repository
bind-mounted at /work (read-only is fine). It reuses the same Python deps as the
indexer image and shells out to our existing scripts to keep behavior consistent.

Environment:
- FASTMCP_HOST (default: 0.0.0.0)
- FASTMCP_INDEXER_PORT (default: 8001)
- QDRANT_URL (e.g., http://qdrant:6333) — server expects Qdrant reachable via this env
- COLLECTION_NAME (default: codebase) — unified collection for seamless cross-repo search

Conventions:
- Repo content must be mounted at /work inside containers
- Clients must not send null values for tool args; omit field or pass empty string ""
- To index repo root: use qdrant_index_root with no args, or qdrant_index with subdir=""

Note: We use the fastmcp library for quick SSE hosting. If you change to another
MCP server framework, keep the tool names and args stable.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# CRITICAL: OpenLit must be initialized BEFORE any qdrant_client imports
# to properly instrument vector DB calls. This import must come first!
# ---------------------------------------------------------------------------
import logging
import os as _os
import sys as _sys

logger = logging.getLogger(__name__)
_roots_env = _os.environ.get("WORK_ROOTS", "")
_roots = [p.strip() for p in _roots_env.split(",") if p.strip()] or ["/work", "/app"]
for _root in _roots:
    if _root and _root not in _sys.path:
        _sys.path.insert(0, _root)

# Now import OpenLit init (before any other scripts imports)
try:
    from scripts import openlit_init  # noqa: F401 - triggers early instrumentation
except ImportError:
    pass  # OpenLit not available
import json
import asyncio
import re
import uuid

# Prefer orjson for faster serialization (2-3x speedup on large payloads)
try:
    import orjson
    def _json_dumps(obj) -> str:
        return orjson.dumps(obj).decode("utf-8")
    def _json_dumps_bytes(obj) -> bytes:
        return orjson.dumps(obj)
except ImportError:
    orjson = None  # type: ignore
    def _json_dumps(obj) -> str:
        return json.dumps(obj)
    def _json_dumps_bytes(obj) -> bytes:
        return json.dumps(obj).encode("utf-8")

import os
import subprocess
import threading
import time
from typing import Any, Dict, Optional, List, Tuple

from pathlib import Path
import sys

# Import structured logging and error handling (after sys.path setup)
# Will be imported after sys.path is configured below

import contextlib

# Ensure code roots are on sys.path so absolute imports like 'from scripts.x import y' work
# when this file is executed directly (sys.path[0] may be /work/scripts).
# Supports multiple roots via WORK_ROOTS env (comma-separated), defaults to /work and /app.
_roots_env = os.environ.get("WORK_ROOTS", "")
_roots = [p.strip() for p in _roots_env.split(",") if p.strip()] or ["/work", "/app"]
try:
    for _root in _roots:
        if _root and _root not in sys.path:
            sys.path.insert(0, _root)
except Exception as e:
    logger.debug(f"Suppressed exception: {e}")

# Note: OpenLit initialization is handled by early import of scripts.openlit_init
# at the top of this file (before any qdrant_client imports)

# Session state imported from mcp_workspace shim (-> scripts.mcp.workspace)
# Must be after sys.path setup
from scripts.mcp_workspace import (
    _MEM_COLL_CACHE,
    SESSION_DEFAULTS,
    SESSION_DEFAULTS_BY_SESSION,
    _SESSION_LOCK,
    _SESSION_CTX_LOCK,
)

# Import structured logging and error handling (after sys.path setup)
try:
    from scripts.logger import (
        get_logger,
        ContextLogger,
        RetrievalError,
        IndexingError,
        DecoderError,
        ValidationError,
        ConfigurationError,
        safe_int,
        safe_float,
        safe_bool,
    )

    logger = get_logger(__name__)
except ImportError:
    # Fallback if logger module not available
    import logging

    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)

    # Import safe conversion functions from utils (single source of truth)
    from scripts.mcp_impl.utils import safe_int, safe_float, safe_bool


from scripts.mcp_auth import (
    require_auth_session as _require_auth_session,
    require_collection_access as _require_collection_access,
    AUTH_HEADER_TOKEN as _AUTH_HEADER_TOKEN,
)

# ---------------------------------------------------------------------------
# Re-exports from extracted modules (backwards compatibility)
# ---------------------------------------------------------------------------
from scripts.mcp_utils import (
    _coerce_bool,
    _coerce_int,
    _coerce_str,
    _coerce_value_string,
    _maybe_parse_jsonish,
    _looks_jsonish_string,
    _parse_kv_string,
    _extract_kwargs_payload,
    _to_str_list_relaxed,
    _split_ident,
    _tokens_from_queries,
    _STOP,
    _env_overrides,
    _primary_identifier_from_queries,
)

from scripts.mcp_toon import (
    _is_toon_output_enabled,
    _should_use_toon,
    _format_results_as_toon,
    _format_context_results_as_toon,
)

# Import implementations from extracted modules
from scripts.mcp_impl.context_search import _context_search_impl
from scripts.mcp_impl.query_expand import _expand_query_impl
from scripts.mcp_impl.search import _repo_search_impl
from scripts.mcp_impl.info_request import (
    _extract_symbols_from_query,
    _extract_related_concepts,
    _format_information_field,
    _extract_relationships,
    _calculate_confidence,
)
from scripts.mcp_impl.admin_tools import _collection_map_impl
from scripts.mcp_impl.search_specialized import (
    _search_tests_for_impl,
    _search_config_for_impl,
    _search_callers_for_impl,
    _search_importers_for_impl,
)
from scripts.mcp_impl.search_history import (
    _search_commits_for_impl,
    _change_history_for_path_impl,
)
from scripts.mcp_impl.symbol_graph import (
    _symbol_graph_impl,
    _format_symbol_graph_toon,
)
from scripts.mcp_impl.pattern_search import _pattern_search_impl

# Shared utilities (lex hashing, snippet highlighter)
try:
    from scripts.utils import highlight_snippet as _do_highlight_snippet
except Exception as e:
    logger.warning(f"Failed to import rich for syntax highlighting: {e}")
    _do_highlight_snippet = None  # fallback guarded at call site


# Back-compat shim for tests expecting _highlight_snippet in this module
# Delegates to scripts.utils.highlight_snippet when available
try:

    def _highlight_snippet(snippet, tokens):  # type: ignore
        return (
            _do_highlight_snippet(snippet, tokens) if _do_highlight_snippet else snippet
        )
except Exception:

    def _highlight_snippet(snippet, tokens):  # type: ignore
        return snippet


try:
    from mcp.server.fastmcp import FastMCP, Context  # type: ignore
except Exception as e:  # pragma: no cover
    raise SystemExit("mcp package is required inside the container: pip install mcp")

# TransportSecuritySettings only exists in mcp >= 1.x with transport_security module
try:
    from mcp.server.transport_security import TransportSecuritySettings  # type: ignore
except ImportError:
    TransportSecuritySettings = None  # type: ignore

APP_NAME = os.environ.get("FASTMCP_SERVER_NAME", "qdrant-indexer-mcp")
HOST = os.environ.get("FASTMCP_HOST", "0.0.0.0")
PORT = safe_int(
    os.environ.get("FASTMCP_INDEXER_PORT", "8001"),
    default=8001,
    logger=logger,
    context="FASTMCP_INDEXER_PORT",
)

# Note: _env_overrides and _primary_identifier_from_queries are now imported from scripts.mcp_impl.utils

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
DEFAULT_COLLECTION = (
    os.environ.get("DEFAULT_COLLECTION")
    or os.environ.get("COLLECTION_NAME")
    or "codebase"
)
try:
    from scripts.workspace_state import get_collection_name as _ws_get_collection_name  # type: ignore

    if DEFAULT_COLLECTION in {"", "default-collection", "my-collection", "codebase"}:
        resolved = _ws_get_collection_name(None)
        if resolved:
            DEFAULT_COLLECTION = resolved
except Exception as e:
    logger.debug(f"Suppressed exception: {e}")

MAX_LOG_TAIL = safe_int(
    os.environ.get("MCP_MAX_LOG_TAIL", "4000"),
    default=4000,
    logger=logger,
    context="MCP_MAX_LOG_TAIL",
)
SNIPPET_MAX_BYTES = safe_int(
    os.environ.get("MCP_SNIPPET_MAX_BYTES", "8192"),
    default=8192,
    logger=logger,
    context="MCP_SNIPPET_MAX_BYTES",
)

MCP_TOOL_TIMEOUT_SECS = safe_float(
    os.environ.get("MCP_TOOL_TIMEOUT_SECS", "3600"),
    default=3600.0,
    logger=logger,
    context="MCP_TOOL_TIMEOUT_SECS",
)

# Set default environment variables for context_answer functionality
# DEBUG_CONTEXT_ANSWER defaults to 0 for production; enable explicitly if needed
os.environ.setdefault("DEBUG_CONTEXT_ANSWER", "0")
os.environ.setdefault("REFRAG_DECODER", "1")
os.environ.setdefault("LLAMACPP_URL", "http://localhost:8080")
os.environ.setdefault("USE_GPU_DECODER", "0")
os.environ.setdefault(
    "CTX_REQUIRE_IDENTIFIER", "0"
)  # Disable strict identifier requirement


# --- TOON functions imported from scripts.mcp_toon ---
# (see imports at top of file for backwards compatibility re-exports)

# --- Workspace state functions imported from mcp_workspace shim ---
from scripts.mcp_workspace import (
    _state_file_path,
    _read_ws_state,
    _default_collection,
    _work_script,
)

TOOLS_METADATA: dict[str, dict] = {
    "repo_search": {
        "name": "repo_search",
        "category": "search",
        "primary_use": "Hybrid semantic + lexical code search",
        "choose_when": [
            "Finding code related to a concept",
            "Starting a search without knowing which tool",
            "Need flexible filtering by language/path/symbol",
        ],
        "choose_instead": {
            "symbol_graph": "Need precise caller/definition relationships",
            "context_answer": "Need an explanation, not raw results",
            "search_tests_for": "Specifically want test files",
        },
        "parameters": {
            "essential": ["query"],
            "common": ["limit", "language", "under", "include_snippet"],
            "advanced": ["rerank_enabled", "output_format", "compact", "mode"],
        },
        "returns": {
            "ok": "bool",
            "results": "list[{score, path, symbol, start_line, end_line, snippet?}]",
            "total": "int",
        },
        "related_tools": ["code_search", "context_search", "info_request"],
        "performance": {
            "typical_latency_ms": (100, 2000),
            "requires_index": True,
            "requires_decoder": False,
        },
    },
    "context_answer": {
        "name": "context_answer",
        "category": "answer",
        "primary_use": "LLM-generated answers with code citations",
        "choose_when": [
            "Need an explanation of how code works",
            "Asking 'how does X work?' questions",
            "Want synthesized answer with sources",
        ],
        "choose_instead": {
            "repo_search": "Want raw code results, not explanation",
            "symbol_graph": "Need precise relationships",
        },
        "parameters": {
            "essential": ["query"],
            "common": ["limit", "language", "under", "include_snippet"],
            "advanced": ["max_tokens", "temperature", "expand", "budget_tokens"],
        },
        "returns": {
            "ok": "bool",
            "answer": "str",
            "citations": "list[{id, path, start_line, end_line}]",
        },
        "related_tools": ["repo_search", "context_search"],
        "performance": {
            "typical_latency_ms": (1000, 10000),
            "requires_index": True,
            "requires_decoder": True,
        },
    },
    "symbol_graph": {
        "name": "symbol_graph",
        "category": "graph",
        "primary_use": "AST-backed symbol relationship queries",
        "choose_when": [
            "Need 'who calls function X'",
            "Need 'where is X defined'",
            "Need 'what imports module Y'",
            "Doing refactoring impact analysis",
        ],
        "choose_instead": {
            "repo_search": "Want conceptual search, not precise relationships",
            "search_callers_for": "Quick text search is sufficient",
        },
        "parameters": {
            "essential": ["symbol", "query_type"],
            "common": ["limit", "language", "under", "repo"],
            "advanced": ["depth", "output_format"],
        },
        "returns": {
            "ok": "bool",
            "results": "list[{path, start_line, end_line, symbol, snippet}]",
            "count": "int",
        },
        "related_tools": ["search_callers_for", "search_importers_for"],
        "performance": {
            "typical_latency_ms": (50, 500),
            "requires_index": True,
            "requires_decoder": False,
        },
    },
    "context_search": {
        "name": "context_search",
        "category": "search",
        "primary_use": "Blend code search with memory retrieval",
        "choose_when": [
            "Want code AND stored memories together",
            "Searching for documented decisions",
            "Need context from team knowledge",
        ],
        "choose_instead": {
            "repo_search": "Only want code, no memories",
            "memory_find": "Only want memories, no code",
        },
        "parameters": {
            "essential": ["query"],
            "common": ["include_memories", "memory_weight", "limit"],
            "advanced": ["per_source_limits", "rerank_enabled"],
        },
        "returns": {
            "ok": "bool",
            "results": "list[{source, score, path|content, ...}]",
            "total": "int",
        },
        "related_tools": ["repo_search", "memory_find"],
        "performance": {
            "typical_latency_ms": (200, 3000),
            "requires_index": True,
            "requires_decoder": False,
        },
    },
    "info_request": {
        "name": "info_request",
        "category": "search",
        "primary_use": "Simplified code discovery with explanations",
        "choose_when": [
            "Want simple single-parameter search",
            "Need human-readable result descriptions",
            "Building minimal integrations",
        ],
        "choose_instead": {
            "repo_search": "Need full control over parameters",
            "context_answer": "Need LLM-generated explanation",
        },
        "parameters": {
            "essential": ["info_request"],
            "common": ["limit", "language", "include_explanation"],
            "advanced": ["include_relationships", "output_format"],
        },
        "returns": {
            "ok": "bool",
            "results": "list[{information, relevance_score, path, ...}]",
            "summary?": "str",
            "related_concepts?": "list[str]",
        },
        "related_tools": ["repo_search", "context_answer"],
        "performance": {
            "typical_latency_ms": (100, 2000),
            "requires_index": True,
            "requires_decoder": False,
        },
    },
    "pattern_search": {
        "name": "pattern_search",
        "category": "search",
        "primary_use": "Structural code pattern matching",
        "choose_when": [
            "Have code example, find similar",
            "Cross-language pattern search",
            "Find structural duplicates",
        ],
        "choose_instead": {
            "repo_search": "Searching by concept, not structure",
            "symbol_graph": "Looking for relationships",
        },
        "parameters": {
            "essential": ["query"],
            "common": ["language", "limit", "target_languages"],
            "advanced": ["query_mode", "aroma_rerank", "min_score"],
        },
        "returns": {
            "ok": "bool",
            "results": "list[{path, start_line, end_line, score, language}]",
            "query_mode": "str",
        },
        "related_tools": ["repo_search"],
        "performance": {
            "typical_latency_ms": (200, 3000),
            "requires_index": True,
            "requires_decoder": False,
        },
    },
    "search_tests_for": {
        "name": "search_tests_for",
        "category": "specialized",
        "primary_use": "Find test files for a feature/function",
        "choose_when": ["Specifically want test files", "Looking for test coverage"],
        "choose_instead": {"repo_search": "Want all code, not just tests"},
        "parameters": {
            "essential": ["query"],
            "common": ["limit", "language", "under"],
            "advanced": ["include_snippet", "compact"],
        },
        "returns": {"ok": "bool", "results": "list[...]", "total": "int"},
        "related_tools": ["repo_search"],
        "performance": {
            "typical_latency_ms": (100, 1500),
            "requires_index": True,
            "requires_decoder": False,
        },
    },
    "search_config_for": {
        "name": "search_config_for",
        "category": "specialized",
        "primary_use": "Find configuration files",
        "choose_when": ["Looking for config files", "Finding settings/options"],
        "choose_instead": {"repo_search": "Want all code, not just config"},
        "parameters": {
            "essential": ["query"],
            "common": ["limit", "under"],
            "advanced": ["include_snippet", "compact"],
        },
        "returns": {"ok": "bool", "results": "list[...]", "total": "int"},
        "related_tools": ["repo_search"],
        "performance": {
            "typical_latency_ms": (100, 1500),
            "requires_index": True,
            "requires_decoder": False,
        },
    },
    "search_callers_for": {
        "name": "search_callers_for",
        "category": "specialized",
        "primary_use": "Text-based search for symbol callers",
        "choose_when": ["Quick caller search is sufficient", "No graph index available"],
        "choose_instead": {"symbol_graph": "Need precise AST-backed callers"},
        "parameters": {
            "essential": ["query"],
            "common": ["limit", "language"],
            "advanced": [],
        },
        "returns": {"ok": "bool", "results": "list[...]", "total": "int"},
        "related_tools": ["symbol_graph"],
        "performance": {
            "typical_latency_ms": (100, 1500),
            "requires_index": True,
            "requires_decoder": False,
        },
    },
    "search_importers_for": {
        "name": "search_importers_for",
        "category": "specialized",
        "primary_use": "Text-based search for module importers",
        "choose_when": ["Quick import search is sufficient", "No graph index available"],
        "choose_instead": {"symbol_graph": "Need precise AST-backed importers"},
        "parameters": {
            "essential": ["query"],
            "common": ["limit", "language"],
            "advanced": [],
        },
        "returns": {"ok": "bool", "results": "list[...]", "total": "int"},
        "related_tools": ["symbol_graph"],
        "performance": {
            "typical_latency_ms": (100, 1500),
            "requires_index": True,
            "requires_decoder": False,
        },
    },
}

# Disable DNS rebinding protection - breaks Docker internal networking (Host: mcp:8000)
_security_settings = (
    TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if TransportSecuritySettings
    else None
)
mcp = FastMCP(APP_NAME, transport_security=_security_settings)

class _AuthHeaderASGIMiddleware:
    """Pure ASGI middleware that extracts Authorization header into context var."""
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
            else:
                token = auth_header.strip() if auth_header else ""
            _AUTH_HEADER_TOKEN.set(token)
        return await self.app(scope, receive, send)


def _add_auth_middleware():
    """Wrap FastMCP's ASGI app with auth header extraction middleware.
    
    FastMCP calls streamable_http_app() or sse_app() to create the Starlette app.
    We patch these methods to wrap the returned app with our middleware.
    """
    logger.info("Setting up auth header middleware...")
    try:
        # Patch streamable_http_app
        if hasattr(mcp, "streamable_http_app"):
            _orig_streamable = mcp.streamable_http_app
            def _patched_streamable(*args, **kwargs):
                app = _orig_streamable(*args, **kwargs)
                logger.info(f"Wrapping streamable_http_app with auth middleware")
                return _AuthHeaderASGIMiddleware(app)
            mcp.streamable_http_app = _patched_streamable
        
        # Patch sse_app for SSE transport
        if hasattr(mcp, "sse_app"):
            _orig_sse = mcp.sse_app
            def _patched_sse(*args, **kwargs):
                app = _orig_sse(*args, **kwargs)
                logger.info(f"Wrapping sse_app with auth middleware")
                return _AuthHeaderASGIMiddleware(app)
            mcp.sse_app = _patched_sse
        
        logger.info("Patched FastMCP app factory methods for auth middleware injection")
    except Exception as e:
        logger.warning(f"Failed to patch FastMCP for auth middleware: {e}")


# Capture tool registry automatically by wrapping the decorator once
_TOOLS_REGISTRY: list[dict] = []
try:
    _orig_tool = mcp.tool

    def _tool_capture_wrapper(*dargs, **dkwargs):
        orig_deco = _orig_tool(*dargs, **dkwargs)

        def _inner(fn):
            try:
                _TOOLS_REGISTRY.append(
                    {
                        "name": dkwargs.get("name") or getattr(fn, "__name__", ""),
                        "description": (getattr(fn, "__doc__", None) or "").strip(),
                    }
                )
            except (AttributeError, TypeError) as e:
                logger.warning(f"Failed to capture tool metadata for {fn}", exc_info=e)
            return orig_deco(fn)

        return _inner

    mcp.tool = _tool_capture_wrapper  # type: ignore
except (AttributeError, TypeError) as e:
    logger.warning("Failed to wrap mcp.tool decorator", exc_info=e)


def _relax_var_kwarg_defaults() -> None:
    """Allow tools that rely on **kwargs compatibility shims to be invoked without
    callers supplying an explicit 'kwargs' or 'arguments' field."""
    try:
        from pydantic_core import PydanticUndefined as _PydanticUndefined  # type: ignore
    except Exception:  # pragma: no cover - defensive

        class _Sentinel:  # type: ignore
            pass

        _PydanticUndefined = _Sentinel()  # type: ignore

    try:
        tool_manager = getattr(mcp, "_tool_manager", None)
        tools = getattr(tool_manager, "_tools", {}) if tool_manager is not None else {}
    except Exception:
        tools = {}

    for tool in tools.values():
        try:
            model = getattr(tool.fn_metadata, "arg_model", None)
            if model is None:
                continue
            fields = getattr(model, "model_fields", {})
            changed = False
            for key in ("kwargs", "arguments"):
                fld = fields.get(key)
                if fld is None:
                    continue
                default = getattr(fld, "default", None)
                default_factory = getattr(fld, "default_factory", None)
                if default is _PydanticUndefined and default_factory is None:
                    try:
                        fld.default_factory = dict  # type: ignore[attr-defined]
                    except Exception:
                        fld.default_factory = lambda: {}  # type: ignore
                    fld.default = None
                    changed = True
            if changed:
                try:
                    model.model_rebuild(force=True)
                except Exception as e:
                    logger.debug(f"Suppressed exception: {e}")
        except Exception as e:
            logger.debug(f"Suppressed exception, continuing: {e}")
            continue


# Lightweight readiness endpoint on a separate health port (non-MCP), optional
# Exposes GET /readyz returning {ok: true, app: <name>} once process is up.
HEALTH_PORT = safe_int(
    os.environ.get("FASTMCP_HEALTH_PORT", "18001"),
    default=18001,
    logger=logger,
    context="FASTMCP_HEALTH_PORT",
)


def _start_readyz_server():
    try:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    if self.path == "/readyz":
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        payload = {"ok": True, "app": APP_NAME}
                        self.wfile.write(_json_dumps_bytes(payload))
                    elif self.path == "/health/warmup":
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        try:
                            from scripts.warm_start import get_warmup_status
                            warmup_status = get_warmup_status()
                        except Exception:
                            warmup_status = {"status": "unknown", "latency_ms": None}
                        payload = {"ok": True, **warmup_status}
                        self.wfile.write(_json_dumps_bytes(payload))
                    elif self.path == "/tools":
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        tools = _TOOLS_REGISTRY
                        try:
                            from scripts.refrag_llamacpp import is_decoder_enabled  # type: ignore
                        except Exception:
                            is_decoder_enabled = lambda: False  # type: ignore
                        try:
                            if not is_decoder_enabled():
                                tools = [
                                    t
                                    for t in tools
                                    if (t.get("name") or "") != "expand_query"
                                ]
                        except Exception as e:
                            logger.debug(f"Suppressed exception: {e}")
                        enriched = []
                        for t in tools:
                            name = t.get("name", "")
                            meta = TOOLS_METADATA.get(name, {})
                            enriched.append({**t, **meta})
                        payload = {"ok": True, "tools": enriched, "metadata": TOOLS_METADATA}
                        self.wfile.write(_json_dumps_bytes(payload))
                    else:
                        self.send_response(404)
                        self.end_headers()
                except Exception:
                    try:
                        self.send_response(500)
                        self.end_headers()
                    except Exception as e:
                        logger.debug(f"Suppressed exception: {e}")

            def log_message(self, *args, **kwargs):
                # Quiet health server logs
                return

        srv = HTTPServer((HOST, HEALTH_PORT), H)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        return True
    except Exception:
        return False


# Import the new subprocess manager
try:
    from scripts.subprocess_manager import run_subprocess_async
except ImportError:
    # Fallback if subprocess_manager not available
    logger.warning("subprocess_manager not available, using fallback implementation")

    async def run_subprocess_async(
        cmd: List[str],
        timeout: Optional[float] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Fallback subprocess runner if subprocess_manager is not available."""
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            # Default timeout from env if not provided by caller
            if timeout is None:
                timeout = MCP_TOOL_TIMEOUT_SECS
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                code = proc.returncode
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception as e:
                    logger.debug(f"Suppressed exception: {e}")
                return {
                    "ok": False,
                    "code": -1,
                    "stdout": "",
                    "stderr": f"Command timed out after {timeout}s",
                }
            stdout = (stdout_b or b"").decode("utf-8", errors="ignore")
            stderr = (stderr_b or b"").decode("utf-8", errors="ignore")

            def _cap_tail(s: str) -> str:
                if not s:
                    return s
                return (
                    s
                    if len(s) <= MAX_LOG_TAIL
                    else ("...[tail truncated]\n" + s[-MAX_LOG_TAIL:])
                )

            return {
                "ok": code == 0,
                "code": code,
                "stdout": _cap_tail(stdout),
                "stderr": _cap_tail(stderr),
            }
        except Exception as e:
            return {"ok": False, "code": -2, "stdout": "", "stderr": str(e)}
        finally:
            try:
                if proc is not None:
                    if proc.stdout is not None:
                        proc.stdout.close()
                    if proc.stderr is not None:
                        proc.stderr.close()
                    # Ensure the process is reaped
                    with contextlib.suppress(Exception):
                        await proc.wait()
            except Exception as e:
                logger.debug(f"Suppressed exception: {e}")


# --- Admin tool helpers imported from mcp_admin_tools shim ---
from scripts.mcp_admin_tools import (
    _EMBED_MODEL_CACHE,
    _EMBED_MODEL_LOCKS,
    _run_async,
    _get_embedding_model,
    _invalidate_router_scratchpad,
    _detect_current_repo,
)

# Lenient argument normalization to tolerate buggy clients (e.g., JSON-in-kwargs, booleans where strings expected)
# Note: _maybe_parse_jsonish and other parsing helpers are now imported from scripts.mcp_utils
from typing import Any as _Any, Dict as _Dict

# Extra parsing helpers for quirky clients that send stringified kwargs
import urllib.parse as _urlparse, ast as _ast

# --- Utility functions imported from scripts.mcp_utils ---
# (see imports at top of file for backwards compatibility re-exports:
#  _parse_kv_string, _coerce_value_string, _to_str_list_relaxed,
#  _extract_kwargs_payload, _looks_jsonish_string, _coerce_bool,
#  _coerce_int, _coerce_str, _STOP, _split_ident, _tokens_from_queries)


@mcp.tool()
async def qdrant_index_root(
    recreate: Optional[bool] = None, collection: Optional[str] = None, session: Optional[str] = None
) -> Dict[str, Any]:
    """Initialize or refresh the vector index for the workspace root (/work).

    When to use:
    - First-time setup for a repo, or to reindex the whole workspace
    - After large refactors or schema changes (set recreate=true)
    - If you want a clean collection or to switch the target collection

    Parameters:
    - recreate: bool (default: false). Drop/recreate the collection before indexing.
    - collection: str (optional). Target collection; defaults to workspace state or env COLLECTION_NAME.

    Returns: subprocess result from ingest_code.py with args echoed. On success code==0.
    Notes:
    - Omit fields instead of sending null values.
    - Safe to call repeatedly; unchanged files are skipped by the indexer.
    """
    sess = _require_auth_session(session)

    # Leniency: if clients embed JSON in 'collection' (and include 'recreate'), parse it
    try:
        if _looks_jsonish_string(collection):
            _parsed = _maybe_parse_jsonish(collection)
            if isinstance(_parsed, dict):
                collection = _parsed.get("collection", collection)
                if recreate is None and "recreate" in _parsed:
                    recreate = _coerce_bool(_parsed.get("recreate"), False)
    except Exception as e:
        logger.debug(f"Suppressed exception: {e}")

    # Resolve collection: prefer explicit value; otherwise use workspace state
    try:
        _c = (collection or "").strip()
    except Exception:
        _c = ""
    # Empty string means use workspace state default (codebase)
    if _c:
        coll = _c
    else:
        try:
            from scripts.workspace_state import (
                get_collection_name as _ws_get_collection_name,
                is_multi_repo_mode as _ws_is_multi_repo_mode,
            )  # type: ignore

            if _ws_is_multi_repo_mode():
                coll = _ws_get_collection_name("/work") or _default_collection()
            else:
                coll = _ws_get_collection_name(None) or _default_collection()
        except Exception:
            coll = _default_collection()

    _require_collection_access((sess or {}).get("user_id") if sess else None, coll, "write")

    env = os.environ.copy()
    env["QDRANT_URL"] = QDRANT_URL
    env["COLLECTION_NAME"] = coll

    cmd = ["python", _work_script("ingest_code.py"), "--root", "/work"]
    if recreate:
        cmd.append("--recreate")

    res = await _run_async(cmd, env=env)
    ret = {"args": {"root": "/work", "collection": coll, "recreate": recreate}, **res}
    try:
        if ret.get("ok") and int(ret.get("code", 1)) == 0:
            if _invalidate_router_scratchpad("/work"):
                ret["invalidated_router_scratchpad"] = True
    except Exception as e:
        logger.debug(f"Suppressed exception: {e}")
    return ret


@mcp.tool()
async def qdrant_list(kwargs: Any = None) -> Dict[str, Any]:
    """List available Qdrant collections.

    When to use:
    - Inspect which collections exist before indexing/searching
    - Debug collection naming in multi-workspace setups

    Parameters:
    - (none). Extra params are ignored.

    Returns:
    - {"collections": [str, ...]} or {"error": "..."}
    """
    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(
            url=QDRANT_URL,
            api_key=os.environ.get("QDRANT_API_KEY"),
            timeout=float(os.environ.get("QDRANT_TIMEOUT", "20") or 20),
        )
        cols_info = await asyncio.to_thread(client.get_collections)
        return {"collections": [c.name for c in cols_info.collections]}
    except ImportError:
        return {"error": "qdrant_client is not installed in this container"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def workspace_info(
    workspace_path: Optional[str] = None, kwargs: Any = None
) -> Dict[str, Any]:
    """Read .codebase/state.json for the current workspace and resolve defaults.

    When to use:
    - Determine the default collection used by this workspace
    - Inspect indexing status and metadata saved by indexer/watch

    Parameters:
    - workspace_path: str (optional). Defaults to "/work".

    Returns:
    - {"workspace_path": str, "default_collection": str, "source": "state_file"|"env", "state": dict}
    """
    ws_path = (workspace_path or "/work").strip() or "/work"


    st = _read_ws_state(ws_path) or {}
    coll = (
        (st.get("qdrant_collection") if isinstance(st, dict) else None)
        or os.environ.get("DEFAULT_COLLECTION")
        or os.environ.get("COLLECTION_NAME")
        or DEFAULT_COLLECTION
    )
    return {
        "workspace_path": ws_path,
        "default_collection": coll,
        "source": ("state_file" if st else "env"),
        "state": st or {},
    }


@mcp.tool()
async def list_workspaces(search_root: Optional[str] = None) -> Dict[str, Any]:
    """Scan search_root recursively for .codebase/state.json and summarize workspaces.

    When to use:
    - Multi-repo environments; pick a workspace/collection to operate on

    Parameters:
    - search_root: str (optional). Directory to scan; defaults to parent of /work.

    Returns:
    - {"workspaces": [{"workspace_path": str, "collection_name": str, "last_updated": str|int, "indexing_state": str}, ...]}
    """
    try:
        from scripts.workspace_state import list_workspaces as _lw  # type: ignore

        items = await asyncio.to_thread(lambda: _lw(search_root))
        return {"workspaces": items}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# collection_map - thin wrapper delegating to _collection_map_impl
# ---------------------------------------------------------------------------
@mcp.tool()
async def collection_map(
    search_root: Optional[str] = None,
    collection: Optional[str] = None,
    repo_name: Optional[str] = None,
    include_samples: Optional[bool] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Return collection↔repo mappings with optional Qdrant payload samples."""
    return await _collection_map_impl(
        search_root=search_root,
        collection=collection,
        repo_name=repo_name,
        include_samples=include_samples,
        limit=limit,
        coerce_bool_fn=_coerce_bool,
    )


@mcp.tool()
async def qdrant_status(
    collection: Optional[str] = None,
    max_points: Optional[int] = None,
    batch: Optional[int] = None,
    kwargs: Any = None,
) -> Dict[str, Any]:
    """Summarize collection size and recent index timestamps.

    When to use:
    - Check whether indexing ran recently and overall point count

    Parameters:
    - collection: str (optional). Defaults to env COLLECTION_NAME.
    - max_points: int. Cap scanned points when estimating timestamps (default 5000).
    - batch: int. Scroll page size (default 1000).

    Returns:
    - {"collection": str, "count": int, "scanned_points": int,
       "last_ingested_at": {"unix": int, "iso": str},
       "last_modified_at": {"unix": int, "iso": str}}
    - or {"error": "..."}
    """
    # Leniency: absorb 'kwargs' JSON payload some clients send instead of top-level args
    try:
        _extra = _extract_kwargs_payload(kwargs)
        if _extra and not collection:
            collection = _extra.get("collection", collection)
        if _extra and max_points in (None, "") and _extra.get("max_points") is not None:
            max_points = _coerce_int(_extra.get("max_points"), None)
        if _extra and batch in (None, "") and _extra.get("batch") is not None:
            batch = _coerce_int(_extra.get("batch"), None)
    except Exception as e:
        logger.debug(f"Suppressed exception: {e}")
    coll = collection or _default_collection()
    try:
        from qdrant_client import QdrantClient
        import datetime as _dt

        client = QdrantClient(
            url=QDRANT_URL,
            api_key=os.environ.get("QDRANT_API_KEY"),
            timeout=float(os.environ.get("QDRANT_TIMEOUT", "20") or 20),
        )
        # Count points
        try:
            cnt_res = await asyncio.to_thread(
                lambda: client.count(collection_name=coll, exact=True)
            )
            total = int(getattr(cnt_res, "count", 0))
        except Exception:
            total = 0
        # Scan a limited number of points to estimate last timestamps
        max_points = (
            int(max_points)
            if max_points not in (None, "")
            else int(os.environ.get("MCP_STATUS_MAX_POINTS", "5000"))
        )
        batch = int(batch) if batch not in (None, "") else 1000
        scanned = 0
        last_ing = None
        last_mod = None
        next_page = None
        while scanned < max_points:
            limit = min(batch, max_points - scanned)
            try:
                pts, next_page = await asyncio.to_thread(
                    lambda: client.scroll(
                        collection_name=coll,
                        limit=limit,
                        offset=next_page,
                        with_payload=True,
                        with_vectors=False,
                    )
                )
            except Exception:
                # Fallback without offset keyword (older clients)
                pts, next_page = await asyncio.to_thread(
                    lambda: client.scroll(
                        collection_name=coll,
                        limit=limit,
                        with_payload=True,
                        with_vectors=False,
                    )
                )
            if not pts:
                break
            scanned += len(pts)
            for p in pts:
                md = (p.payload or {}).get("metadata") or {}
                ti = md.get("ingested_at")
                tm = md.get("last_modified_at")
                if isinstance(ti, int):
                    last_ing = ti if last_ing is None else max(last_ing, ti)
                if isinstance(tm, int):
                    last_mod = tm if last_mod is None else max(last_mod, tm)
            if not next_page:
                break

        def _iso(ts):
            if isinstance(ts, int) and ts > 0:
                try:
                    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat()
                except Exception:
                    return ""
            return ""

        return {
            "collection": coll,
            "count": total,
            "scanned_points": scanned,
            "last_ingested_at": {"unix": last_ing or 0, "iso": _iso(last_ing)},
            "last_modified_at": {"unix": last_mod or 0, "iso": _iso(last_mod)},
        }
    except Exception as e:
        return {"collection": coll, "error": str(e)}


@mcp.tool()
async def warmup_status(
    **kwargs: Any,
) -> Dict[str, Any]:
    """Get current warmup status for models (embedding + reranker).

    Returns:
    - {"status": "cold"|"warming"|"warm"|"failed", "embedding_ms": float, "reranker_ms": float, "total_ms": float}
    - or {"status": "failed", "error": "..."}
    """
    try:
        from scripts.warm_start import get_warmup_status
        return get_warmup_status()
    except Exception as e:
        return {"status": "unknown", "error": str(e)}


@mcp.tool()
async def qdrant_index(
    subdir: Optional[str] = None,
    recreate: Optional[bool] = None,
    collection: Optional[str] = None,
    session: Optional[str] = None,
) -> Dict[str, Any]:
    """Index the workspace (/work) or a specific subdirectory.

    Use this when you want to index only part of the repo (e.g., "scripts" or "backend/api").
    For full-repo indexing, prefer qdrant_index_root.

    Parameters:
    - subdir: str. "" or omit to index the root; or a relative path under /work (e.g., "scripts").
    - recreate: bool (default: false). Drop/recreate the collection before indexing.
    - collection: str (optional). Target collection; defaults to workspace state or env COLLECTION_NAME.

    Returns: subprocess result from ingest_code.py with args echoed. On success code==0.
    Notes:
    - Paths are sandboxed to /work; attempts to escape will be rejected.
    - Omit fields rather than sending null values.
    """
    sess = _require_auth_session(session)

    # Leniency: parse JSON-ish payloads mistakenly sent in 'collection' or 'subdir'
    try:
        if _looks_jsonish_string(collection):
            _parsed = _maybe_parse_jsonish(collection)
            if isinstance(_parsed, dict):
                subdir = _parsed.get("subdir", subdir)
                collection = _parsed.get("collection", collection)
                if recreate is None and "recreate" in _parsed:
                    recreate = _coerce_bool(_parsed.get("recreate"), False)
        if _looks_jsonish_string(subdir):
            _parsed2 = _maybe_parse_jsonish(subdir)
            if isinstance(_parsed2, dict):
                subdir = _parsed2.get("subdir", subdir)
                collection = _parsed2.get("collection", collection)
                if recreate is None and "recreate" in _parsed2:
                    recreate = _coerce_bool(_parsed2.get("recreate"), False)
    except Exception as e:
        logger.debug(f"Suppressed exception: {e}")

    root = "/work"
    if subdir:
        subdir = subdir.lstrip("/")
        root = os.path.join(root, subdir)
    # Enforce /work sandbox
    real_root = os.path.realpath(root)
    if not (real_root == "/work" or real_root.startswith("/work/")):
        return {"ok": False, "error": "subdir escapes /work sandbox"}
    root = real_root
    # Resolve collection: prefer explicit value; otherwise use workspace state (use workspace root)
    try:
        _c2 = (collection or "").strip()
    except Exception:
        _c2 = ""
    # Empty string means use workspace state default (codebase)
    if _c2:
        coll = _c2
    else:
        try:
            from scripts.workspace_state import (
                get_collection_name as _ws_get_collection_name,
                is_multi_repo_mode as _ws_is_multi_repo_mode,
            )  # type: ignore

            if _ws_is_multi_repo_mode():
                coll = _ws_get_collection_name(root) or _default_collection()
            else:
                coll = _ws_get_collection_name(None) or _default_collection()
        except Exception:
            coll = _default_collection()

    _require_collection_access((sess or {}).get("user_id") if sess else None, coll, "write")

    env = os.environ.copy()
    env["QDRANT_URL"] = QDRANT_URL
    env["COLLECTION_NAME"] = coll

    cmd = [
        "python",
        _work_script("ingest_code.py"),
        "--root",
        root,
    ]
    if recreate:
        cmd.append("--recreate")

    res = await _run_async(cmd, env=env)
    ret = {"args": {"root": root, "collection": coll, "recreate": recreate}, **res}
    try:
        if ret.get("ok") and int(ret.get("code", 1)) == 0:
            if _invalidate_router_scratchpad("/work"):
                ret["invalidated_router_scratchpad"] = True
    except Exception as e:
        logger.debug(f"Suppressed exception: {e}")
    return ret


@mcp.tool()
async def set_session_defaults(
    collection: Any = None,
    mode: Any = None,
    under: Any = None,
    language: Any = None,
    repo: Any = None,
    compact: Any = None,
    output_format: Any = None,
    include_snippet: Any = None,
    rerank_enabled: Any = None,
    limit: Any = None,
    session: Any = None,
    ctx: Context = None,
    **kwargs,
) -> Dict[str, Any]:
    """Set defaults (e.g., collection, mode, under) for subsequent calls.

    Behavior:
    - If request Context is available, persist defaults per-connection so later calls on
      the same MCP session automatically use them (no token required).
    - Optionally also stores token-scoped defaults for cross-connection reuse.

    Parameters:
    - collection: Default collection name
    - mode: Search mode hint
    - under: Default path prefix filter
    - language: Default language filter
    - repo: Default repo filter for multi-repo setups
    - compact: Default compact response mode (bool)
    - output_format: Default output format ("json" or "toon")
    - include_snippet: Default snippet inclusion (bool)
    - rerank_enabled: Default reranking toggle (bool)
    - limit: Default result limit (int)
    - session: Session token for cross-connection reuse
    """
    try:
        _extra = _extract_kwargs_payload(kwargs)
        if _extra:
            if (collection is None or (isinstance(collection, str) and collection.strip() == "")) and _extra.get("collection") is not None:
                collection = _extra.get("collection")
            if (mode is None or (isinstance(mode, str) and str(mode).strip() == "")) and _extra.get("mode") is not None:
                mode = _extra.get("mode")
            if (under is None or (isinstance(under, str) and str(under).strip() == "")) and _extra.get("under") is not None:
                under = _extra.get("under")
            if (language is None or (isinstance(language, str) and str(language).strip() == "")) and _extra.get("language") is not None:
                language = _extra.get("language")
            if (session is None or (isinstance(session, str) and str(session).strip() == "")) and _extra.get("session") is not None:
                session = _extra.get("session")
            if repo is None and _extra.get("repo") is not None:
                repo = _extra.get("repo")
            if compact is None and _extra.get("compact") is not None:
                compact = _extra.get("compact")
            if output_format is None and _extra.get("output_format") is not None:
                output_format = _extra.get("output_format")
            if include_snippet is None and _extra.get("include_snippet") is not None:
                include_snippet = _extra.get("include_snippet")
            if rerank_enabled is None and _extra.get("rerank_enabled") is not None:
                rerank_enabled = _extra.get("rerank_enabled")
            if limit is None and _extra.get("limit") is not None:
                limit = _extra.get("limit")
    except Exception as e:
        logger.debug(f"Suppressed exception: {e}")

    defaults: Dict[str, Any] = {}
    unset_keys: set[str] = set()
    for _key, _val in (("collection", collection), ("mode", mode), ("under", under), ("language", language)):
        if isinstance(_val, str):
            _s = _val.strip()
            if _s:
                defaults[_key] = _s
            else:
                unset_keys.add(_key)
    if isinstance(repo, str) and repo.strip():
        defaults["repo"] = repo.strip()
    elif isinstance(repo, list):
        defaults["repo"] = repo
    if isinstance(output_format, str) and output_format.strip():
        defaults["output_format"] = output_format.strip()
    if compact is not None:
        defaults["compact"] = bool(compact) if not isinstance(compact, bool) else compact
    if include_snippet is not None:
        defaults["include_snippet"] = bool(include_snippet) if not isinstance(include_snippet, bool) else include_snippet
    if rerank_enabled is not None:
        defaults["rerank_enabled"] = bool(rerank_enabled) if not isinstance(rerank_enabled, bool) else rerank_enabled
    if limit is not None:
        try:
            defaults["limit"] = int(limit)
        except (ValueError, TypeError):
            pass

    # Per-connection storage (preferred)
    try:
        if ctx is not None and getattr(ctx, "session", None) is not None and (defaults or unset_keys):
            with _SESSION_CTX_LOCK:
                existing2 = SESSION_DEFAULTS_BY_SESSION.get(ctx.session) or {}
                for _k in unset_keys:
                    existing2.pop(_k, None)
                existing2.update(defaults)
                SESSION_DEFAULTS_BY_SESSION[ctx.session] = existing2
    except Exception as e:
        logger.debug(f"Suppressed exception: {e}")

    # Optional token storage
    sid = str(session).strip() if session is not None else ""
    if not sid:
        sid = uuid.uuid4().hex[:12]
    try:
        if defaults or unset_keys:
            with _SESSION_LOCK:
                existing = SESSION_DEFAULTS.get(sid) or {}
                for _k in unset_keys:
                    existing.pop(_k, None)
                existing.update(defaults)
                SESSION_DEFAULTS[sid] = existing
    except Exception as e:
        logger.debug(f"Suppressed exception: {e}")

    return {
        "ok": True,
        "session": sid,
        "defaults": SESSION_DEFAULTS.get(sid, {}),
        "applied": ("connection" if (ctx is not None and getattr(ctx, "session", None) is not None) else "token"),
    }

@mcp.tool()
async def qdrant_prune(kwargs: Any = None, **ignored: Any) -> Dict[str, Any]:
    """Remove stale points for /work (files deleted/moved but still in the index).

    Extra arguments are accepted for forward compatibility but ignored.
    Returns the subprocess result from ``prune.py`` with status information.
    """
    env = os.environ.copy()
    env["PRUNE_ROOT"] = "/work"

    cmd = ["python", _work_script("prune.py")]
    res = await _run_async(cmd, env=env)
    return res


@mcp.tool()
async def graph_backfill(
    collection: Optional[str] = None,
    repo: Optional[str] = None,
    max_points: Optional[int] = None,
    max_iterations: Optional[int] = None,
    session: Optional[str] = None,
) -> Dict[str, Any]:
    """Populate Neo4j graph edges from existing indexed code.

    Use this after enabling NEO4J_GRAPH=1 or to rebuild graph from scratch.
    Safe to run multiple times - processes incremental batches.

    Parameters:
    - collection: str (optional). Target collection; defaults to workspace state or env COLLECTION_NAME.
    - repo: str (optional). Filter by repository name.
    - max_points: int (optional). Max points per iteration (default: 1000).
    - max_iterations: int (optional). Max iterations (default: 50).

    Returns: dict with processed count, iterations, and status.
    """
    from qdrant_client import QdrantClient
    from scripts.ingest.pipeline import graph_backfill_tick

    sess = _require_auth_session(session)

    _c = (collection or "").strip()
    if _c:
        coll = _c
    else:
        try:
            from scripts.workspace_state import (
                get_collection_name as _ws_get_collection_name,
                is_multi_repo_mode as _ws_is_multi_repo_mode,
            )
            if _ws_is_multi_repo_mode():
                coll = _ws_get_collection_name("/work") or _default_collection()
            else:
                coll = _ws_get_collection_name(None) or _default_collection()
        except Exception:
            coll = _default_collection()

    _require_collection_access((sess or {}).get("user_id") if sess else None, coll, "read")

    max_pts = safe_int(max_points, default=1000, logger=logger, context="max_points")
    max_iter = safe_int(max_iterations, default=50, logger=logger, context="max_iterations")

    client = QdrantClient(url=QDRANT_URL)

    total_processed = 0
    iterations_run = 0

    try:
        for i in range(max_iter):
            processed = graph_backfill_tick(
                client,
                coll,
                repo_name=repo,
                max_points=max_pts,
            )
            iterations_run += 1
            total_processed += processed

            if processed == 0:
                break
    except Exception as e:
        logger.warning(f"Graph backfill error: {e}", exc_info=True)
        return {
            "ok": False,
            "error": str(e),
            "collection": coll,
            "processed": total_processed,
            "iterations": iterations_run,
        }

    return {
        "ok": True,
        "collection": coll,
        "processed": total_processed,
        "iterations": iterations_run,
        "complete": iterations_run < max_iter,
    }


# ---------------------------------------------------------------------------
# Code signal detection imported from mcp_code_signals shim
# ---------------------------------------------------------------------------
from scripts.mcp_code_signals import (
    _CODE_INTENT_CACHE,
    _CODE_INTENT_LOCK,
    _CODE_QUERY_ARCHETYPES,
    _PROSE_QUERY_ARCHETYPES,
    _CODE_SIGNAL_PATTERNS,
    _CODE_KEYWORDS,
    _init_code_intent_centroids,
    _detect_code_intent_embedding,
    _detect_code_signals,
)


# ---------------------------------------------------------------------------
# repo_search - thin wrapper delegating to _repo_search_impl
# ---------------------------------------------------------------------------
@mcp.tool()
async def repo_search(
    query: Any = None,
    queries: Any = None,
    limit: Any = None,
    per_path: Any = None,
    include_snippet: Any = None,
    context_lines: Any = None,
    rerank_enabled: Any = None,
    rerank_top_n: Any = None,
    rerank_return_m: Any = None,
    rerank_timeout_ms: Any = None,
    highlight_snippet: Any = None,
    collection: Any = None,
    workspace_path: Any = None,
    mode: Any = None,
    session: Any = None,
    ctx: Context = None,
    language: Any = None,
    under: Any = None,
    kind: Any = None,
    symbol: Any = None,
    path_regex: Any = None,
    path_glob: Any = None,
    not_glob: Any = None,
    ext: Any = None,
    not_: Any = None,
    case: Any = None,
    repo: Any = None,
    compact: Any = None,
    output_format: Any = None,
    lean: Any = None,
    args: Any = None,
    kwargs: Any = None,
) -> Dict[str, Any]:
    """Primary hybrid semantic + lexical code search across the repository.

    PRIMARY USE: Find code spans matching a natural language concept or topic.

    CHOOSE THIS WHEN:
    - You need to find code related to a concept (e.g., "authentication", "caching")
    - You want to locate implementations, not just definitions
    - You need flexible filtering by language, path, or symbol
    - You want the best balance of recall and precision
    - You're starting a search and aren't sure which specific tool to use

    CHOOSE INSTEAD:
    - symbol_graph -> when you need "who calls X" or "where is X defined" (AST-backed)
    - context_answer -> when you need an EXPLANATION, not raw code results
    - context_search -> when you want to blend code results with stored memories
    - search_tests_for -> when specifically looking for test files
    - search_config_for -> when specifically looking for config files
    - pattern_search -> when searching by code structure/pattern across languages

    QUERY EXAMPLES:
    Good queries (natural language, conceptual):
      "authentication middleware"     - finds auth-related code
      "error handling with retry"     - finds retry logic
      "database connection pooling"   - finds connection management
      "user session management"       - finds session-related code
      "API rate limiting"             - finds rate limit implementations
      "caching layer implementation"  - finds cache logic
      "websocket message handling"    - finds WS handlers
      "file upload processing"        - finds upload logic

    Bad queries (will return poor results):
      "auth OR login OR session"      - boolean operators NOT supported
      "def.*authenticate"             - regex NOT supported in query
      "*.py with class User"          - glob syntax NOT for query field
      "function"                      - too vague, be more specific
      "get"                           - too generic
      "the code that handles the thing" - unclear intent

    ESSENTIAL PARAMETERS:
    - query (str | list[str]): Natural language description of what you're looking for.
      Multiple queries are fused for broader recall.

    COMMON PARAMETERS:
    - limit (int, default=10): Maximum results to return.
    - per_path (int, default=2): Max results per file. Increase for thorough search.
    - include_snippet (bool, default=True): Include code snippets in results.
    - language (str): Filter by language ("python", "typescript", "go", etc.)
    - under (str): Restrict to directory path ("scripts/", "src/api/")
    - symbol (str): Filter by symbol name (function, class, method)
    - path_glob (str | list[str]): File pattern filter ("**/*.py", "src/**")
    - repo (str | list[str]): Filter by repo name(s). Use "*" for all repos.

    ADVANCED PARAMETERS:
    - rerank_enabled (bool, default=True): ONNX cross-encoder reranking for relevance.
    - rerank_top_n (int, default=20): Candidates to rerank. Increase for benchmarks.
    - output_format (str): "json" (default) or "toon" for token-efficient format.
    - compact (bool, default=False): Strip verbose fields for minimal response.
    - mode (str): "code_first", "docs_first", "balanced", or "dense" (pure embedding).
    - not_glob (str | list[str]): Exclude paths matching pattern.
    - not_ (str): Exclude results containing this text.
    - case (str): "sensitive" for case-sensitive matching.

    RETURNS:
    {
        "ok": true,
        "results": [
            {
                "score": 0.85,           // Relevance score (0-1+)
                "path": "src/auth.py",   // File path
                "symbol": "authenticate", // Symbol name if available
                "start_line": 42,        // Start line number
                "end_line": 67,          // End line number
                "snippet": "def auth..." // Code snippet (if include_snippet=true)
            }
        ],
        "total": 5,                      // Total results returned
        "used_rerank": true,             // Whether reranking was applied
        "rerank_counters": {...}         // Reranking statistics
    }

    PERFORMANCE TIPS:
    - Use language filter to reduce search space and improve relevance
    - Use under filter when you know the general code area
    - Set include_snippet=false if you only need file locations
    - Set compact=true to reduce response size for large result sets
    """
    return await _repo_search_impl(
        query=query,
        queries=queries,
        limit=limit,
        per_path=per_path,
        include_snippet=include_snippet,
        context_lines=context_lines,
        rerank_enabled=rerank_enabled,
        rerank_top_n=rerank_top_n,
        rerank_return_m=rerank_return_m,
        rerank_timeout_ms=rerank_timeout_ms,
        highlight_snippet=highlight_snippet,
        collection=collection,
        workspace_path=workspace_path,
        mode=mode,
        session=session,
        ctx=ctx,
        language=language,
        under=under,
        kind=kind,
        symbol=symbol,
        path_regex=path_regex,
        path_glob=path_glob,
        not_glob=not_glob,
        ext=ext,
        not_=not_,
        case=case,
        repo=repo,
        compact=compact,
        output_format=output_format,
        lean=lean,
        args=args,
        kwargs=kwargs,
        get_embedding_model_fn=_get_embedding_model,
        require_auth_session_fn=_require_auth_session,
        do_highlight_snippet_fn=_do_highlight_snippet,
        run_async_fn=_run_async,
    )


@mcp.tool()
async def repo_search_compat(arguments: Any = None, **kwargs) -> Dict[str, Any]:
    """Compatibility wrapper for repo_search (lenient argument handling).

    When to use:
    - Clients that only send a single dict payload or use aliases (q/text/top_k)
    - Avoids schema errors by normalizing and forwarding to repo_search

    Returns: same shape as repo_search.
    Note: Prefer calling repo_search directly when possible.
    """
    try:
        # Handle both: arguments={...} dict OR **kwargs spread
        args = arguments if isinstance(arguments, dict) else (kwargs or {})
        # Core query: prefer explicit query, else q/text; allow queries list passthrough
        query = args.get("query") or args.get("q") or args.get("text")
        queries = args.get("queries")
        # top_k alias for limit
        limit = args.get("limit")
        if (
            limit is None or (isinstance(limit, str) and str(limit).strip() == "")
        ) and ("top_k" in args):
            limit = args.get("top_k")
        # not/ not_ normalization
        not_value = args.get("not_") if ("not_" in args) else args.get("not")

        # Build forward kwargs; pass alias keys too so repo_search's leniency picks them up
        forward = {
            "query": query,
            "limit": limit,
            "per_path": args.get("per_path"),
            "include_snippet": args.get("include_snippet"),
            "context_lines": args.get("context_lines"),
            "rerank_enabled": args.get("rerank_enabled"),
            "rerank_top_n": args.get("rerank_top_n"),
            "rerank_return_m": args.get("rerank_return_m"),
            "rerank_timeout_ms": args.get("rerank_timeout_ms"),
            "highlight_snippet": args.get("highlight_snippet"),
            "collection": args.get("collection"),
            "session": args.get("session"),
            "workspace_path": args.get("workspace_path"),
            "language": args.get("language"),
            "under": args.get("under"),
            "kind": args.get("kind"),
            "symbol": args.get("symbol"),
            "path_regex": args.get("path_regex"),
            "path_glob": args.get("path_glob"),
            "not_glob": args.get("not_glob"),
            "ext": args.get("ext"),
            "not_": not_value,
            "case": args.get("case"),
            "compact": args.get("compact"),
            "mode": args.get("mode"),
            "repo": args.get("repo"),  # Cross-codebase isolation
            "output_format": args.get("output_format"),  # "json" or "toon"
            "lean": args.get("lean"),  # Token optimization for agents
            "queries": queries,
        }
        # Drop Nones to avoid overriding repo_search defaults unnecessarily
        clean = {k: v for k, v in forward.items() if v is not None}
        return await repo_search(**clean)
    except Exception as e:
        return {"error": f"repo_search_compat failed: {e}"}


@mcp.tool()
async def context_answer_compat(arguments: Any = None) -> Dict[str, Any]:
    """Compatibility wrapper for context_answer (lenient argument handling).

    When to use:
    - Clients that send a single 'arguments' dict or alternate keys (q/text)
    - Avoids schema errors by normalizing/forwarding to context_answer

    Returns: same shape as context_answer.
    Note: Prefer calling context_answer directly when possible.
    """
    try:
        args = arguments or {}
        query = args.get("query") or args.get("q") or args.get("text")
        forward = {
            "query": query,
            "limit": args.get("limit"),
            "per_path": args.get("per_path"),
            "budget_tokens": args.get("budget_tokens"),
            "include_snippet": args.get("include_snippet"),
            "collection": args.get("collection"),
            "max_tokens": args.get("max_tokens"),
            "temperature": args.get("temperature"),
            "mode": args.get("mode"),
            "expand": args.get("expand"),
            # ---- Forward retrieval filters so router hints are honored ----
            "language": args.get("language"),
            "under": args.get("under"),
            "kind": args.get("kind"),
            "symbol": args.get("symbol"),
            "ext": args.get("ext"),
            "path_regex": args.get("path_regex"),
            "path_glob": args.get("path_glob"),
            "not_glob": args.get("not_glob"),
            "case": args.get("case"),
            # pass through NOT filter under either key
            "not_": args.get("not_") or args.get("not"),
        }
        clean = {k: v for k, v in forward.items() if v is not None}
        return await context_answer(**clean)
    except Exception as e:
        return {"error": f"context_answer_compat failed: {e}"}


# ---------------------------------------------------------------------------
# Specialized search tools - thin wrappers delegating to extracted impls
# ---------------------------------------------------------------------------
@mcp.tool()
async def search_tests_for(
    query: Any = None,
    limit: Any = None,
    include_snippet: Any = None,
    context_lines: Any = None,
    under: Any = None,
    language: Any = None,
    session: Any = None,
    compact: Any = None,
    kwargs: Any = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Find test files related to a query.

    PRIMARY USE: Quickly find tests for a feature, function, or module.
    Convenience wrapper that presets common test file patterns.

    CHOOSE THIS WHEN:
    - You specifically want TEST files, not implementation code
    - You're looking for tests related to a feature
    - You want to find test coverage for a function/class
    - You're exploring how something is tested

    CHOOSE INSTEAD:
    - repo_search -> when you want ALL code, not just tests
    - symbol_graph -> when you need "what tests call function X"

    QUERY EXAMPLES:
    Good queries (feature/function focused):
      "user authentication"           - finds tests for auth features
      "database connection"           - finds DB connection tests
      "API rate limiting"             - finds rate limit tests
      "email sending"                 - finds email-related tests
      "input validation"              - finds validation tests
      "UserService"                   - finds tests for UserService class

    Bad queries:
      "all tests"                     - too broad
      "test_*.py"                     - glob pattern, use path_glob param
      "pass"                          - assertion keyword, not meaningful
      "def test_"                     - code fragment, use repo_search

    ESSENTIAL PARAMETERS:
    - query (str | list[str]): Natural language description of what you want tests for.

    COMMON PARAMETERS:
    - limit (int, default=10): Maximum results to return.
    - include_snippet (bool, default=True): Include test code snippets.
    - context_lines (int): Lines of context around matches.
    - under (str): Restrict to directory path (e.g., "tests/unit/").
    - language (str): Filter by language.
    - compact (bool): Minimal response fields.

    PRESET GLOBS (automatically applied):
    - tests/**
    - test/**
    - **/*test*.*
    - **/*_test.*
    - **/Test*/**

    RETURNS: Same schema as repo_search.
    {
        "ok": true,
        "results": [
            {
                "score": 0.82,
                "path": "tests/test_auth.py",
                "symbol": "test_authenticate_valid_user",
                "start_line": 45,
                "end_line": 58,
                "snippet": "def test_authenticate_valid_user():..."
            }
        ],
        "total": 8
    }

    USAGE PATTERNS:
    # Find tests for authentication
    search_tests_for(query="authentication")

    # Find tests in a specific directory
    search_tests_for(query="database", under="tests/integration/")

    # Find Python tests only
    search_tests_for(query="caching", language="python")
    """
    return await _search_tests_for_impl(
        query=query,
        limit=limit,
        include_snippet=include_snippet,
        context_lines=context_lines,
        under=under,
        language=language,
        session=session,
        compact=compact,
        kwargs=kwargs,
        ctx=ctx,
        repo_search_fn=repo_search,
    )


@mcp.tool()
async def search_config_for(
    query: Any = None,
    limit: Any = None,
    include_snippet: Any = None,
    context_lines: Any = None,
    under: Any = None,
    session: Any = None,
    compact: Any = None,
    kwargs: Any = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Find configuration files related to a query.

    PRIMARY USE: Quickly find config files for a service, feature, or setting.
    Convenience wrapper that presets common config file patterns.

    CHOOSE THIS WHEN:
    - You need to find configuration for a service/feature
    - You're looking for environment variables, settings, or options
    - You want to find where something is configured
    - You're debugging configuration issues

    CHOOSE INSTEAD:
    - repo_search -> when you want ALL code, not just config files
    - search_tests_for -> when looking for test files

    QUERY EXAMPLES:
    Good queries (service/setting focused):
      "database connection"           - finds DB config files
      "authentication settings"       - finds auth config
      "logging configuration"         - finds logging setup
      "API keys"                      - finds key config (careful with secrets!)
      "environment variables"         - finds env config
      "redis cache"                   - finds Redis config
      "docker compose"                - finds Docker config

    Bad queries:
      "*.yaml"                        - glob pattern, handled by presets
      "config"                        - too vague
      "settings"                      - too generic
      "json"                          - file format, not a query

    ESSENTIAL PARAMETERS:
    - query (str | list[str]): Natural language description of what config you need.

    COMMON PARAMETERS:
    - limit (int, default=10): Maximum results to return.
    - include_snippet (bool, default=True): Include config content snippets.
    - context_lines (int): Lines of context around matches.
    - under (str): Restrict to directory path.
    - compact (bool): Minimal response fields.

    PRESET GLOBS (automatically applied):
    - **/*.yml, **/*.yaml
    - **/*.json
    - **/*.toml
    - **/*.ini
    - **/*.env
    - **/*.config, **/*.conf
    - **/*.properties
    - **/*.csproj, **/*.props, **/*.targets
    - **/*.xml
    - **/appsettings*.json

    RETURNS: Same schema as repo_search.
    {
        "ok": true,
        "results": [
            {
                "score": 0.85,
                "path": "config/database.yml",
                "start_line": 12,
                "end_line": 25,
                "snippet": "database:\\n  host: localhost\\n  port: 5432..."
            }
        ],
        "total": 5
    }

    USAGE PATTERNS:
    # Find database config
    search_config_for(query="database connection")

    # Find Docker configuration
    search_config_for(query="docker service ports")

    # Find in specific directory
    search_config_for(query="api settings", under="config/")

    WARNING: Config files may contain sensitive data (API keys, passwords).
    Be cautious about exposing results that might contain secrets.
    """
    return await _search_config_for_impl(
        query=query,
        limit=limit,
        include_snippet=include_snippet,
        context_lines=context_lines,
        under=under,
        session=session,
        compact=compact,
        kwargs=kwargs,
        ctx=ctx,
        repo_search_fn=repo_search,
    )


@mcp.tool()
async def search_callers_for(
    query: Any = None,
    limit: Any = None,
    language: Any = None,
    session: Any = None,
    kwargs: Any = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Heuristic text-based search for callers/usages of a symbol.

    PRIMARY USE: Find files that likely call or reference a function/class.
    Uses text search, not AST analysis - faster but less precise than symbol_graph.

    CHOOSE THIS WHEN:
    - You want a quick, broad search for symbol references
    - You're okay with some false positives in exchange for speed
    - The codebase doesn't have graph index built yet
    - You want to find textual mentions, not just actual calls

    CHOOSE INSTEAD:
    - symbol_graph with query_type="callers" -> for PRECISE AST-backed caller analysis
    - repo_search -> when you want full control over search parameters

    QUERY EXAMPLES:
    Good queries (symbol names):
      "authenticate"                  - finds references to authenticate
      "UserService"                   - finds references to UserService
      "validate_input"                - finds references to validate_input
      "CacheManager.get"              - finds references to CacheManager.get

    Bad queries:
      "who calls authenticate"        - use symbol_graph for this phrasing
      "find all usages of X"          - use symbol_graph
      "authentication"                - concept, not symbol name

    ESSENTIAL PARAMETERS:
    - query (str): Symbol name to find callers/references for.

    COMMON PARAMETERS:
    - limit (int, default=10): Maximum results to return.
    - language (str): Filter by language for more relevant results.

    RETURNS: Same schema as repo_search.

    COMPARISON WITH symbol_graph:
    | Aspect | search_callers_for | symbol_graph |
    |--------|-------------------|--------------|
    | Method | Text search | AST analysis |
    | Speed | Faster | Slower |
    | Precision | Lower (false positives) | Higher (actual calls) |
    | Requires | Nothing special | Graph index |
    | Use for | Quick exploration | Precise refactoring |

    USAGE PATTERNS:
    # Quick reference search
    search_callers_for(query="authenticate", language="python")

    # For precise caller analysis, prefer:
    symbol_graph(symbol="authenticate", query_type="callers")
    """
    return await _search_callers_for_impl(
        query=query,
        limit=limit,
        language=language,
        session=session,
        kwargs=kwargs,
        ctx=ctx,
        repo_search_fn=repo_search,
    )


@mcp.tool()
async def search_importers_for(
    query: Any = None,
    limit: Any = None,
    language: Any = None,
    session: Any = None,
    kwargs: Any = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Heuristic text-based search for files importing a module/symbol.

    PRIMARY USE: Find files that likely import a module or symbol.
    Uses text search, not AST analysis - faster but less precise than symbol_graph.

    CHOOSE THIS WHEN:
    - You want a quick search for import statements
    - You're looking for textual import/require/use mentions
    - The codebase doesn't have graph index built yet
    - You want approximate results quickly

    CHOOSE INSTEAD:
    - symbol_graph with query_type="importers" -> for PRECISE AST-backed import analysis
    - repo_search -> when you want full control over search parameters

    QUERY EXAMPLES:
    Good queries (module/symbol names):
      "auth_utils"                    - finds imports of auth_utils
      "CacheManager"                  - finds imports of CacheManager
      "qdrant_client"                 - finds imports of qdrant_client
      "express"                       - finds require('express')
      "pandas"                        - finds import pandas

    Bad queries:
      "what imports X"                - use symbol_graph for this phrasing
      "import statements"             - too vague
      "from ... import"               - syntax, not a module name

    ESSENTIAL PARAMETERS:
    - query (str): Module or symbol name to find importers for.

    COMMON PARAMETERS:
    - limit (int, default=10): Maximum results to return.
    - language (str): Filter by language for more relevant results.

    PRESET GLOBS (automatically applied):
    Code files across all common languages (*.py, *.js, *.ts, *.go, etc.)

    RETURNS: Same schema as repo_search.

    COMPARISON WITH symbol_graph:
    | Aspect | search_importers_for | symbol_graph |
    |--------|---------------------|--------------|
    | Method | Text search | AST analysis |
    | Speed | Faster | Slower |
    | Precision | Lower (false positives) | Higher (actual imports) |
    | Requires | Nothing special | Graph index |
    | Use for | Quick exploration | Precise dependency analysis |

    USAGE PATTERNS:
    # Quick import search
    search_importers_for(query="qdrant_client", language="python")

    # For precise import analysis, prefer:
    symbol_graph(symbol="qdrant_client", query_type="importers")
    """
    return await _search_importers_for_impl(
        query=query,
        limit=limit,
        language=language,
        session=session,
        kwargs=kwargs,
        ctx=ctx,
        repo_search_fn=repo_search,
    )


@mcp.tool()
async def symbol_graph(
    symbol: str = None,
    query_type: str = "callers",
    limit: Any = None,
    language: Any = None,
    under: Any = None,
    repo: Any = None,
    session: Any = None,
    output_format: Any = None,
    depth: Any = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """AST-backed symbol graph queries for precise code relationships.

    PRIMARY USE: Find WHO CALLS a function, WHERE something is DEFINED,
    or WHAT IMPORTS a module using the pre-built symbol graph.

    CHOOSE THIS WHEN:
    - You need "who calls this function?" (callers)
    - You need "where is this defined?" (definition)
    - You need "what imports this module?" (importers)
    - You need "what does this function call?" (callees)
    - You want PRECISE relationships, not text-based fuzzy matches
    - You're doing refactoring impact analysis

    CHOOSE INSTEAD:
    - repo_search -> when you want CONCEPTUAL search, not precise relationships
    - search_callers_for -> convenience wrapper, uses text search (less precise)
    - search_importers_for -> convenience wrapper, uses text search (less precise)

    QUERY EXAMPLES:

    For "callers" query_type (who calls X?):
      symbol="authenticate"          - finds all callers of authenticate()
      symbol="UserService.get_user"  - finds callers of get_user method
      symbol="validate_input"        - finds where validate_input is called

    For "definition" query_type (where is X defined?):
      symbol="CacheManager"          - finds CacheManager class definition
      symbol="run_hybrid_search"     - finds function definition
      symbol="USER_TIMEOUT"          - finds constant definition

    For "importers" query_type (what imports X?):
      symbol="auth_utils"            - finds files importing auth_utils module
      symbol="CacheManager"          - finds files importing CacheManager
      symbol="qdrant_client"         - finds files importing qdrant_client

    For "callees" query_type (what does X call?):
      symbol="authenticate"          - finds functions called BY authenticate
      symbol="process_request"       - finds all functions process_request calls

    ESSENTIAL PARAMETERS:
    - symbol (str): Symbol name to analyze. Can be:
      - Simple name: "authenticate"
      - Qualified path: "UserService.get_user"
      - Module name: "auth_utils"

    - query_type (str, default="callers"): Type of relationship query:
      - "callers": Find code that CALLS this symbol
      - "definition": Find WHERE this symbol is DEFINED
      - "importers": Find code that IMPORTS this symbol/module
      - "callees": Find what this symbol CALLS (inverse of callers)

    COMMON PARAMETERS:
    - limit (int, default=20): Maximum results to return.
    - depth (int, default=1): Traversal depth for multi-hop queries.
      - depth=1: Direct relationships only
      - depth=2: Callers of callers, callees of callees, etc.
      - depth=3+: Use sparingly, can be expensive
    - language (str): Filter by language.
    - under (str): Filter by path prefix.
    - repo (str): Filter by repository name. Use "*" for all repos.
    - output_format (str): "json" or "toon" for token-efficient format.

    RETURNS:
    {
        "ok": true,
        "results": [
            {
                "path": "src/api/handlers.py",
                "start_line": 142,
                "end_line": 145,
                "symbol": "handle_login",
                "symbol_path": "handlers.handle_login",
                "language": "python",
                "snippet": "    result = authenticate(username, password)",
                "hop": 1,           // For depth>1: which hop found this
                "via": "authenticate"  // For depth>1: intermediate symbol
            }
        ],
        "symbol": "authenticate",
        "query_type": "callers",
        "count": 12,
        "depth": 1,
        "used_graph": true,    // True if graph collection was used (fast)
        "suggestions": [...]   // Fuzzy matches if exact symbol not found
    }

    MULTI-HOP EXAMPLE (depth=2):
    # "Who calls the callers of authenticate?"
    symbol_graph(symbol="authenticate", query_type="callers", depth=2)
    # Returns both direct callers (hop=1) and callers-of-callers (hop=2)

    NOTES:
    - Graph must be indexed (run qdrant_index_root first)
    - For fuzzy matching, suggestions are returned if exact symbol not found
    - Hydration adds code snippets and accurate line numbers automatically
    - Use depth>1 carefully - exponential growth in results
    """
    if not symbol or not str(symbol).strip():
        return {"error": "symbol parameter is required", "results": []}

    _limit = safe_int(limit, default=20, logger=logger, context="symbol_graph.limit")
    _depth = safe_int(depth, default=1, logger=logger, context="symbol_graph.depth")
    _depth = max(1, min(5, _depth))  # Clamp depth to [1, 5]

    result = await _symbol_graph_impl(
        symbol=str(symbol).strip(),
        query_type=query_type or "callers",
        limit=_limit,
        language=str(language).strip() if language else None,
        under=str(under).strip() if under else None,
        repo=str(repo).strip() if repo else None,
        session=str(session).strip() if session else None,
        ctx=ctx,
        depth=_depth,
    )

    # Format output
    use_toon = _should_use_toon(output_format)
    if use_toon:
        return {"text": _format_symbol_graph_toon(result), **result}

    return result


@mcp.tool()
async def search_commits_for(
    query: Any = None,
    path: Any = None,
    collection: Any = None,
    limit: Any = None,
    max_points: Any = None,
) -> Dict[str, Any]:
    """Search git commit history indexed in Qdrant.

    What it does:
    - Queries commit documents ingested by scripts/ingest_history.py
    - Filters by optional file path (metadata.files contains path)

    Parameters:
    - query: str or list[str]; matched lexically against commit message/text
    - path: str (optional). Relative path under /work; filters commits that touched this file
    - collection: str (optional). Defaults to env/WS collection
    - limit: int (optional, default 10). Max commits to return
    - max_points: int (optional). Safety cap on scanned points (default 1000)

    Returns:
    - {"ok": true, "results": [{"commit_id", "author_name", "authored_date", "message", "files"}, ...], "scanned": int}
    - On error: {"ok": false, "error": "..."}
    """
    return await _search_commits_for_impl(
        query=query,
        path=path,
        collection=collection,
        limit=limit,
        max_points=max_points,
        default_collection_fn=_default_collection,
        get_embedding_model_fn=_get_embedding_model,
    )


@mcp.tool()
async def change_history_for_path(
    path: Any,
    collection: Any = None,
    max_points: Any = None,
    include_commits: Any = None,
) -> Dict[str, Any]:
    """Summarize recent change metadata for a file path from the index.

    Parameters:
    - path: str. Relative path under /work.
    - collection: str (optional). Defaults to env/WS default.
    - max_points: int (optional). Safety cap on scanned points.
    - include_commits: bool (optional). If true, attach a small list of recent commits
      touching this path based on the commit index.

    Returns:
    - {"ok": true, "summary": {...}} or {"ok": false, "error": "..."}.
    """
    return await _change_history_for_path_impl(
        path=path,
        collection=collection,
        max_points=max_points,
        include_commits=include_commits,
        default_collection_fn=_default_collection,
        search_commits_fn=search_commits_for,
    )

# --- context_answer helpers imported from mcp_context_answer shim ---
from scripts.mcp_context_answer import (
    _cleanup_answer,
    _answer_style_guidance,
    _strip_preamble_labels,
    _validate_answer_output,
    _ca_unwrap_and_normalize,
    _ca_prepare_filters_and_retrieve,
    _ca_fallback_and_budget,
    _ca_build_citations_and_context,
    _ca_ident_supplement,
    _ca_decoder_params,
    _ca_build_prompt,
    _ca_decode,
    _ca_postprocess_answer,
    _synthesize_from_citations,
    _context_answer_impl,
)


@mcp.tool()
async def context_answer(
    query: Any = None,
    limit: Any = None,
    per_path: Any = None,
    budget_tokens: Any = None,
    include_snippet: Any = None,
    collection: Any = None,
    max_tokens: Any = None,
    temperature: Any = None,
    mode: Any = None,  # "stitch" (default) or "pack"
    expand: Any = None,  # whether to LLM-expand queries (up to 2 alternates)
    # Retrieval filter parameters (passed through to hybrid_search)
    language: Any = None,
    under: Any = None,
    kind: Any = None,
    symbol: Any = None,
    ext: Any = None,
    path_regex: Any = None,
    path_glob: Any = None,
    not_glob: Any = None,
    case: Any = None,
    not_: Any = None,
    # Repo scoping (cross-codebase isolation)
    repo: Any = None,  # str, list[str], or "*" to search all repos
    kwargs: Any = None,
) -> Dict[str, Any]:
    """Generate LLM-powered answers with citations grounded in retrieved code.

    PRIMARY USE: Get an EXPLANATION or ANSWER to a question, not raw search results.
    Uses retrieval-augmented generation (RAG) with a local LLM decoder.

    CHOOSE THIS WHEN:
    - You need an EXPLANATION ("How does X work?", "What is Y?")
    - You want a synthesized answer with source citations
    - You're asking a question that requires understanding, not just finding
    - You want the system to READ code and EXPLAIN it to you

    CHOOSE INSTEAD:
    - repo_search -> when you want RAW CODE RESULTS, not explanations
    - symbol_graph -> when you need precise "who calls X" relationships
    - context_search -> when you want code + memories without LLM synthesis

    QUERY EXAMPLES:
    Good queries (questions requiring explanation):
      "How does the authentication system validate tokens?"
      "What is the purpose of the CacheManager class?"
      "Explain the error handling strategy in the API layer"
      "How are database connections pooled in this project?"
      "What happens when a user session expires?"
      "Describe the data flow for user registration"
      "How does retry logic work in the HTTP client?"

    Bad queries (not suited for LLM answers):
      "find authentication code"      - use repo_search for finding code
      "list all Python files"         - use repo_search with language filter
      "UserController"                - symbol name only, use symbol_graph
      "src/auth.py"                   - file path, just read the file
      "def authenticate"              - code fragment, use repo_search

    ESSENTIAL PARAMETERS:
    - query (str | list[str]): Question or topic requiring explanation.
      Should be phrased as a question or request for explanation.

    RETRIEVAL PARAMETERS:
    - limit (int, default=15): Code spans to retrieve for context.
    - per_path (int, default=5): Max spans per file.
    - budget_tokens (int): Token budget for code context. Default from env.
    - include_snippet (bool, default=True): Include code in response.
    - language (str): Filter retrieval by language.
    - under (str): Restrict retrieval to directory path.
    - repo (str | list[str]): Filter by repo. Use "*" for all repos.

    GENERATION PARAMETERS:
    - max_tokens (int): Max tokens for generated answer.
    - temperature (float): Sampling temperature (0.0-1.0). Lower = more focused.
    - mode (str): Prompt assembly mode. "stitch" (default) or "pack".
    - expand (bool): Use LLM to generate query expansions for better recall.

    COMMON FILTER PARAMETERS (same as repo_search):
    - symbol (str): Filter by symbol name.
    - path_glob (str | list[str]): Filter by file pattern.
    - not_glob (str | list[str]): Exclude file patterns.
    - ext (str): Filter by file extension.

    RETURNS:
    {
        "ok": true,
        "answer": "The authentication system validates tokens by first checking
                   the JWT signature using the secret from config [1], then
                   verifying expiration time [2]. If valid, it extracts the
                   user ID and loads permissions from the database [3].",
        "citations": [
            {
                "id": 1,
                "path": "src/auth/jwt.py",
                "start_line": 45,
                "end_line": 52,
                "snippet": "def verify_token(token):..."  // Optional
            },
            {
                "id": 2,
                "path": "src/auth/jwt.py",
                "start_line": 54,
                "end_line": 58
            },
            {
                "id": 3,
                "path": "src/auth/permissions.py",
                "start_line": 23,
                "end_line": 31
            }
        ],
        "query": ["How does authentication validate tokens"],
        "used": {
            "spans": 5,
            "tokens": 1842
        }
    }

    // On insufficient context:
    {
        "answer": "insufficient context",
        "citations": [],
        "query": [...],
        "hint": "Try broadening your query or checking if the feature exists"
    }

    NOTES:
    - Answers include bracketed citations like [1], [2] referencing the citations array
    - If context is insufficient, returns "insufficient context" as the answer
    - Local LLM decoder must be available (llama.cpp or cloud fallback)
    - Reranking is enabled by default for optimal retrieval quality
    """
    return await _context_answer_impl(
        query=query,
        limit=limit,
        per_path=per_path,
        budget_tokens=budget_tokens,
        include_snippet=include_snippet,
        collection=collection,
        max_tokens=max_tokens,
        temperature=temperature,
        mode=mode,
        expand=expand,
        language=language,
        under=under,
        kind=kind,
        symbol=symbol,
        ext=ext,
        path_regex=path_regex,
        path_glob=path_glob,
        not_glob=not_glob,
        case=case,
        not_=not_,
        repo=repo,
        kwargs=kwargs,
        get_embedding_model_fn=_get_embedding_model,
        expand_query_fn=expand_query,
        prepare_filters_and_retrieve_fn=_ca_prepare_filters_and_retrieve,
    )
 
@mcp.tool()
async def code_search(
    query: Any = None,
    limit: Any = None,
    per_path: Any = None,
    include_snippet: Any = None,
    context_lines: Any = None,
    rerank_enabled: Any = None,
    rerank_top_n: Any = None,
    rerank_return_m: Any = None,
    rerank_timeout_ms: Any = None,
    highlight_snippet: Any = None,
    collection: Any = None,
    language: Any = None,
    under: Any = None,
    kind: Any = None,
    symbol: Any = None,
    path_regex: Any = None,
    path_glob: Any = None,
    not_glob: Any = None,
    ext: Any = None,
    not_: Any = None,
    case: Any = None,
    session: Any = None,
    compact: Any = None,
    # Memory blending (opt-in)
    include_memories: Any = None,
    memory_weight: Any = None,
    per_source_limits: Any = None,
    kwargs: Any = None,
) -> Dict[str, Any]:
    """Alias of repo_search for discoverability. Use repo_search directly.

    PRIMARY USE: This is an EXACT ALIAS of repo_search. Exists for discoverability
    in IDEs and agents that might search for "code_search" instead of "repo_search".

    CHOOSE THIS WHEN:
    - You would use repo_search (they are identical)
    - Your tooling expects a "code_search" function name

    CHOOSE INSTEAD:
    - repo_search -> same functionality, canonical name
    - See repo_search docstring for full documentation

    QUERY EXAMPLES:
    Good queries (natural language, conceptual):
      "authentication middleware"     - finds auth-related code
      "error handling with retry"     - finds retry logic
      "database connection setup"     - finds DB connection code
      "user input validation"         - finds validation logic
      "async task processing"         - finds async patterns

    Bad queries (will return poor results):
      "auth AND login"                - boolean operators NOT supported
      "grep -r 'password'"            - not a shell command
      "class.*Controller"             - regex NOT supported
      "SELECT * FROM users"           - SQL query, not code search
      "https://github.com/..."        - URL, not a search query

    ESSENTIAL PARAMETERS:
    - query (str): Natural language description of code you're looking for.

    All parameters and return format are identical to repo_search.
    See repo_search documentation for complete parameter reference.

    MEMORY BLENDING (opt-in, delegates to context_search):
    - include_memories: bool. If true, blends memory results with code results.
    - memory_weight: float (default 1.0). Scales memory scores relative to code.
    - per_source_limits: dict, e.g. {"code": 5, "memory": 3}

    RETURNS: Same schema as repo_search.
    """
    # If include_memories is requested, delegate to context_search for blending
    # Coerce to bool first to handle string 'false'/'0' from some clients
    if _coerce_bool(include_memories, default=False):
        return await context_search(
            query=query,
            limit=limit,
            per_path=per_path,
            include_memories=include_memories,
            memory_weight=memory_weight,
            per_source_limits=per_source_limits,
            include_snippet=include_snippet,
            context_lines=context_lines,
            rerank_enabled=rerank_enabled,
            rerank_top_n=rerank_top_n,
            rerank_return_m=rerank_return_m,
            rerank_timeout_ms=rerank_timeout_ms,
            highlight_snippet=highlight_snippet,
            collection=collection,
            language=language,
            under=under,
            kind=kind,
            symbol=symbol,
            path_regex=path_regex,
            path_glob=path_glob,
            not_glob=not_glob,
            ext=ext,
            not_=not_,
            case=case,
            session=session,
            compact=compact,
            kwargs=kwargs,
        )
    return await repo_search(
        query=query,
        limit=limit,
        per_path=per_path,
        include_snippet=include_snippet,
        context_lines=context_lines,
        rerank_enabled=rerank_enabled,
        rerank_top_n=rerank_top_n,
        rerank_return_m=rerank_return_m,
        rerank_timeout_ms=rerank_timeout_ms,
        highlight_snippet=highlight_snippet,
        collection=collection,
        language=language,
        under=under,
        kind=kind,
        symbol=symbol,
        path_regex=path_regex,
        path_glob=path_glob,
        not_glob=not_glob,
        ext=ext,
        not_=not_,
        case=case,
        session=session,
        compact=compact,
        kwargs=kwargs,
    )


# ---------------------------------------------------------------------------
# info_request: Simplified codebase retrieval with explanation mode
# (helpers imported from scripts.mcp_impl.info_request)
# ---------------------------------------------------------------------------
@mcp.tool()
async def info_request(
    # Primary parameter
    info_request: str = None,
    information_request: str = None,  # Alias
    # Explanation mode
    include_explanation: bool = None,
    # Relationship mapping
    include_relationships: bool = None,
    # Auth/session (passed through to repo_search)
    session: str = None,
    # Optional filters (pass-through to repo_search)
    limit: int = None,
    language: str = None,
    under: str = None,
    repo: Any = None,
    path_glob: Any = None,
    # Additional options
    include_snippet: bool = None,
    context_lines: int = None,
    # Output format
    output_format: Any = None,  # "json" (default) or "toon" for token-efficient format
    kwargs: Any = None,
) -> Dict[str, Any]:
    """Simplified codebase discovery with optional explanation mode.

    PRIMARY USE: Quick, single-parameter code search with human-readable results.
    Designed as a drop-in replacement for basic "find code about X" queries.

    CHOOSE THIS WHEN:
    - You want a simple, one-parameter search interface
    - You want results with human-readable "information" descriptions
    - You want optional explanation mode for richer context
    - You're building a simple integration and want minimal complexity

    CHOOSE INSTEAD:
    - repo_search -> when you need full control over filtering and parameters
    - context_answer -> when you need an LLM-generated ANSWER, not just results
    - symbol_graph -> when you need precise call/definition relationships

    QUERY EXAMPLES:
    Good queries (natural language descriptions):
      "database connection pooling"     - finds DB connection code
      "authentication middleware"       - finds auth-related code
      "error handling patterns"         - finds error handling logic
      "user input validation"           - finds validation code
      "caching implementation"          - finds cache logic
      "logging configuration"           - finds logging setup
      "API endpoint handlers"           - finds route handlers

    Bad queries (too vague or wrong format):
      "code"                           - too vague
      "the function"                   - unspecific
      "*.py"                           - glob pattern, use path_glob param
      "auth|login"                     - boolean syntax not supported
      "line 42"                        - use file reading for specific lines

    ESSENTIAL PARAMETERS:
    - info_request (str): Natural language description of code you're looking for.
    - information_request (str): Alias for info_request.

    EXPLANATION MODE PARAMETERS:
    - include_explanation (bool, default=False): When true, adds:
      - summary: Brief overview of what was found
      - primary_locations: Key file paths
      - related_concepts: Technical concepts discovered
      - query_understanding: How the query was interpreted

    - include_relationships (bool, default=False): When true, adds to each result:
      - imports_from: Modules this code imports
      - calls: Functions this code calls
      - related_paths: Related files

    COMMON PARAMETERS:
    - limit (int, default=10): Maximum results to return.
    - language (str): Filter by language ("python", "typescript", etc.)
    - under (str): Restrict to directory path.
    - repo (str | list[str]): Filter by repo. Use "*" for all repos.
    - output_format (str): "json" or "toon" for token-efficient format.
    - include_snippet (bool, default=True): Include code snippets.

    RETURNS (compact mode, default):
    {
        "ok": true,
        "results": [
            {
                "information": "Found function 'authenticate' in src/auth.py (lines 42-67)",
                "relevance_score": 0.85,   // Alias for score
                "score": 0.85,
                "path": "src/auth.py",
                "symbol": "authenticate",
                "start_line": 42,
                "end_line": 67
            }
        ],
        "total": 5
    }

    RETURNS (explanation mode, include_explanation=True):
    {
        "ok": true,
        "results": [...],
        "summary": "Found 5 authentication-related functions across 3 files",
        "primary_locations": ["src/auth.py", "src/middleware/auth.py"],
        "related_concepts": ["jwt", "token", "session", "middleware"],
        "query_understanding": "Looking for authentication implementation code",
        "confidence": {
            "level": "high",
            "score": 0.82,
            "symbol_matches": 3
        }
    }

    USAGE PATTERNS:
    # Simple discovery:
    info_request(info_request="database connection")

    # With explanation:
    info_request(
        info_request="authentication flow",
        include_explanation=True,
        include_relationships=True
    )
    """
    # Resolve query from either parameter
    query = info_request or information_request
    if not query or not str(query).strip():
        return {"ok": False, "error": "info_request parameter is required", "results": []}
    query = str(query).strip()

    # Resolve defaults from env
    _default_limit = safe_int(
        os.environ.get("INFO_REQUEST_LIMIT", "10"), default=10, logger=logger
    )
    _default_context = safe_int(
        os.environ.get("INFO_REQUEST_CONTEXT_LINES", "5"), default=5, logger=logger
    )
    _default_explain = str(
        os.environ.get("INFO_REQUEST_EXPLAIN_DEFAULT", "0")
    ).strip().lower() in {"1", "true", "yes", "on"}
    _default_relationships = str(
        os.environ.get("INFO_REQUEST_RELATIONSHIPS", "0")
    ).strip().lower() in {"1", "true", "yes", "on"}

    # Apply defaults
    eff_limit = limit if limit is not None else _default_limit
    eff_context = context_lines if context_lines is not None else _default_context
    eff_snippet = include_snippet if include_snippet is not None else True
    eff_explain = include_explanation if include_explanation is not None else _default_explain
    eff_relationships = include_relationships if include_relationships is not None else _default_relationships

    # Smart limits based on query characteristics (only if user didn't override)
    if limit is None:
        query_words = len(query.split())
        query_lower = query.lower()
        if query_words <= 2:  # Short query like "auth handler"
            eff_limit = 15  # More results for broad queries
        elif "how does" in query_lower or "what is" in query_lower:
            eff_limit = 8   # Questions need focused results

    # Call repo_search (always JSON - we format TOON ourselves after enhancement)
    search_result = await repo_search(
        query=query,
        limit=eff_limit,
        per_path=3,  # Better default for info requests
        session=session,
        include_snippet=eff_snippet,
        context_lines=eff_context,
        language=language,
        under=under,
        repo=repo,
        path_glob=path_glob,
        output_format="json",  # Always get JSON to iterate results
        kwargs=kwargs,
    )

    # Extract results
    results = search_result.get("results", [])
    total = search_result.get("total", len(results))
    used_rerank = search_result.get("used_rerank", False)

    # Enhance each result with information field and optional relationships
    enhanced_results = []
    for r in results:
        enhanced = dict(r)
        enhanced["information"] = _format_information_field(r)
        enhanced["relevance_score"] = r.get("score", 0.0)  # Alias
        # Add relationships if requested
        if eff_relationships:
            enhanced["relationships"] = _extract_relationships(r)
        enhanced_results.append(enhanced)

    # Build better search strategy string
    strategy_parts = ["hybrid"]
    if used_rerank:
        strategy_parts.append("rerank")
    if repo:
        strategy_parts.append("repo_filtered")
    if language:
        strategy_parts.append(f"lang:{language}")
    if under:
        strategy_parts.append("path_filtered")
    search_strategy = "+".join(strategy_parts)

    # Build response
    response: Dict[str, Any] = {
        "ok": True,
        "results": enhanced_results,
        "total": total,
        "search_strategy": search_strategy,
    }

    # Add explanation if requested
    if eff_explain:
        # Primary locations: unique file paths
        seen_paths = set()
        primary_locations = []
        for r in results:
            p = r.get("path", "")
            if p and p not in seen_paths:
                seen_paths.add(p)
                primary_locations.append(p)
                if len(primary_locations) >= 5:
                    break

        # Related concepts
        related_concepts = _extract_related_concepts(query, results)

        # Detected symbols from query
        detected_symbols = _extract_symbols_from_query(query)

        # Summary
        n_files = len(seen_paths)
        summary = f"Found {total} results related to '{query}' across {n_files} file{'s' if n_files != 1 else ''}"

        # Group results by file
        files_map: Dict[str, list] = {}
        for r in enhanced_results:
            p = r.get("path", "")
            if p not in files_map:
                files_map[p] = []
            files_map[p].append({
                "symbol": r.get("symbol", ""),
                "line": r.get("start_line", 0),
                "score": r.get("score", 0.0),
            })

        grouped_results = {
            "by_file": {
                path: {
                    "count": len(items),
                    "top_symbols": [i["symbol"] for i in sorted(items, key=lambda x: -x["score"])[:3] if i["symbol"]],
                }
                for path, items in files_map.items()
            }
        }

        # Calculate confidence
        confidence = _calculate_confidence(query, enhanced_results)

        response["summary"] = summary
        response["primary_locations"] = primary_locations
        response["related_concepts"] = related_concepts
        response["grouped_results"] = grouped_results
        response["confidence"] = confidence
        response["query_understanding"] = {
            "intent": "search_for_code",
            "detected_language": language or None,
            "detected_symbols": detected_symbols,
            "search_strategy": search_strategy,
        }

    # Apply TOON formatting if requested or enabled globally
    if _should_use_toon(output_format):
        return _format_results_as_toon(response, compact=False)  # Keep info_request fields
    return response


# ---------------------------------------------------------------------------
# context_search - thin wrapper delegating to _context_search_impl
# ---------------------------------------------------------------------------
@mcp.tool()
async def context_search(
    query: Any = None,
    limit: Any = None,
    per_path: Any = None,
    include_memories: Any = None,
    memory_weight: Any = None,
    per_source_limits: Any = None,
    include_snippet: Any = None,
    context_lines: Any = None,
    rerank_enabled: Any = None,
    rerank_top_n: Any = None,
    rerank_return_m: Any = None,
    rerank_timeout_ms: Any = None,
    highlight_snippet: Any = None,
    collection: Any = None,
    language: Any = None,
    under: Any = None,
    kind: Any = None,
    symbol: Any = None,
    path_regex: Any = None,
    path_glob: Any = None,
    not_glob: Any = None,
    ext: Any = None,
    not_: Any = None,
    case: Any = None,
    session: Any = None,
    compact: Any = None,
    repo: Any = None,
    output_format: Any = None,
    kwargs: Any = None,
) -> Dict[str, Any]:
    """Blend code search results with memory-store entries for richer context.

    PRIMARY USE: Search code AND retrieve relevant stored memories/notes in one call.

    CHOOSE THIS WHEN:
    - You want code results PLUS relevant memories (notes, docs, decisions)
    - You're searching for something where team knowledge might help
    - You want to surface both implementation AND documentation/context
    - You need to check if there are existing notes about a topic

    CHOOSE INSTEAD:
    - repo_search -> when you ONLY want code results (faster, no memory overhead)
    - context_answer -> when you need an LLM-generated EXPLANATION
    - memory_find -> when you ONLY want memories (no code search)

    QUERY EXAMPLES:
    Good queries (conceptual, topic-based):
      "authentication design decisions"  - finds code + stored auth decisions
      "API versioning strategy"          - finds API code + design notes
      "database migration approach"      - finds migration code + notes
      "caching invalidation policy"      - finds cache code + policy notes
      "error handling conventions"       - finds error code + team standards

    Bad queries (too narrow for memory blending):
      "def authenticate("               - too specific, use repo_search
      "class UserController"            - exact match, use repo_search
      "line 42 of auth.py"              - specific location, just read the file
      "git commit abc123"               - not a search query
      "npm install express"             - command, not a query

    ESSENTIAL PARAMETERS:
    - query (str | list[str]): Natural language description of what you're looking for.

    MEMORY BLENDING PARAMETERS:
    - include_memories (bool, default=False): MUST SET TO TRUE to enable memory blending.
      Without this, context_search behaves identically to repo_search.
    - memory_weight (float, default=1.0): Scale memory scores relative to code.
      Values >1.0 boost memories, <1.0 favor code results.
    - per_source_limits (dict): Control results per source.
      Example: {"code": 6, "memory": 3} returns max 6 code + 3 memory results.

    COMMON PARAMETERS (same as repo_search):
    - limit (int, default=10): Maximum total results.
    - language (str): Filter code results by language.
    - under (str): Restrict code search to directory path.
    - include_snippet (bool, default=True): Include code snippets.
    - rerank_enabled (bool, default=True): Cross-encoder reranking.
    - output_format (str): "json" or "toon" for token-efficient format.
    - repo (str | list[str]): Filter by repo. Use "*" for all repos.

    RETURNS:
    {
        "ok": true,
        "results": [
            {
                "source": "code",         // "code" or "memory"
                "score": 0.85,
                "path": "src/auth.py",    // For code results
                "symbol": "authenticate",
                "start_line": 42,
                "end_line": 67,
                "snippet": "def auth..."
            },
            {
                "source": "memory",       // Memory results have different shape
                "score": 0.78,
                "content": "Auth uses JWT tokens with 24h expiry...",
                "metadata": {"kind": "note", "created_at": "2024-..."}
            }
        ],
        "total": 9,
        "memory_note": "3 memories included"  // Optional note about memory results
    }

    USAGE PATTERN:
    # To blend code + memories (recommended pattern):
    context_search(
        query="authentication architecture",
        include_memories=True,
        per_source_limits={"code": 5, "memory": 3}
    )

    # To search code only (same as repo_search):
    context_search(query="authentication", include_memories=False)
    """
    return await _context_search_impl(
        query=query,
        limit=limit,
        per_path=per_path,
        include_memories=include_memories,
        memory_weight=memory_weight,
        per_source_limits=per_source_limits,
        include_snippet=include_snippet,
        context_lines=context_lines,
        rerank_enabled=rerank_enabled,
        rerank_top_n=rerank_top_n,
        rerank_return_m=rerank_return_m,
        rerank_timeout_ms=rerank_timeout_ms,
        highlight_snippet=highlight_snippet,
        collection=collection,
        language=language,
        under=under,
        kind=kind,
        symbol=symbol,
        path_regex=path_regex,
        path_glob=path_glob,
        not_glob=not_glob,
        ext=ext,
        not_=not_,
        case=case,
        session=session,
        compact=compact,
        repo=repo,
        output_format=output_format,
        kwargs=kwargs,
        repo_search_fn=repo_search,
        get_embedding_model_fn=_get_embedding_model,
    )


# ---------------------------------------------------------------------------
# expand_query - thin wrapper delegating to _expand_query_impl
# ---------------------------------------------------------------------------
@mcp.tool()
async def expand_query(
    query: Any = None,
    max_new: Any = None,
    session: Optional[str] = None,
) -> Dict[str, Any]:
    """LLM-assisted query expansion (local llama.cpp, if enabled).

    When to use:
    - Generate 1–2 compact alternates before repo_search/context_answer

    Parameters:
    - query: str or list[str]
    - max_new: int in [0,5] (default 3)

    Returns:
    - {"alternates": list[str]} or {"alternates": [], "hint": "..."} if decoder disabled
    """
    return await _expand_query_impl(query=query, max_new=max_new, session=session)


# ---------------------------------------------------------------------------
# Pattern Search - Structural code similarity (conditional on PATTERN_VECTORS=1)
# ---------------------------------------------------------------------------
_PATTERN_SEARCH_ENABLED = str(os.environ.get("PATTERN_VECTORS", "")).strip().lower() in {
    "1", "true", "yes", "on"
}

if _PATTERN_SEARCH_ENABLED:
    @mcp.tool()
    async def pattern_search(
        query: Any = None,
        language: Any = None,
        limit: Any = None,
        min_score: Any = None,
        include_snippet: Any = None,
        context_lines: Any = None,
        target_languages: Any = None,
        output_format: Any = None,
        compact: Any = None,
        aroma_rerank: Any = None,
        aroma_alpha: Any = None,
        query_mode: Any = None,
    ) -> Dict[str, Any]:
        """Find structurally similar code patterns across all languages.

        PRIMARY USE: Search by CODE STRUCTURE rather than text/semantics.
        Finds code with similar control flow, API usage, or patterns.

        CHOOSE THIS WHEN:
        - You have a CODE EXAMPLE and want to find similar patterns
        - You want to find code STRUCTURALLY similar (not just textually)
        - You're searching across languages (Python pattern -> find in Go/Rust/Java)
        - You want to detect code duplication based on structure
        - You're searching for patterns like "retry with backoff", "singleton"

        CHOOSE INSTEAD:
        - repo_search -> when searching by CONCEPT, not structural pattern
        - symbol_graph -> when looking for call/definition relationships
        - context_answer -> when you need an EXPLANATION

        QUERY EXAMPLES:

        Code example mode (query_mode="code" or auto-detected):
          "for i in range(3): try: ... except: time.sleep(2**i)"
          "if err != nil { return err }"
          "async function $NAME($$$) { await $EXPR; }"
          "with open(file) as f: data = f.read()"
          "try { ... } catch (e) { console.error(e); throw e; }"

        Description mode (query_mode="description" or auto-detected):
          "retry with exponential backoff"
          "resource cleanup pattern"
          "singleton implementation"
          "factory pattern"
          "decorator wrapping function"
          "error handling with logging"
          "connection pooling"
          "rate limiting implementation"

        Bad queries (wrong use case):
          "authentication code"           - use repo_search for concepts
          "who calls authenticate"        - use symbol_graph
          "explain the auth flow"         - use context_answer
          "files in src/"                 - use glob/file tools

        ESSENTIAL PARAMETERS:
        - query (str): EITHER a code example OR a natural language pattern description.
          The mode is auto-detected, or you can force it with query_mode.

        MODE CONTROL PARAMETERS:
        - query_mode (str, default="auto"): How to interpret the query.
          - "auto": Auto-detect if query is code or description
          - "code": Force interpretation as code example
          - "description": Force interpretation as pattern description
        - language (str): Language hint for code examples. Also triggers code mode
          in auto-detection. Example: "python", "go", "rust", "typescript"

        COMMON PARAMETERS:
        - limit (int, default=10): Maximum results to return.
        - min_score (float, default=0.3): Minimum similarity score threshold.
        - include_snippet (bool, default=True): Include code snippets in results.
        - target_languages (list[str]): Filter results to specific languages.
          Example: ["python", "go"] to find pattern only in Python and Go files.
        - repo (str | list[str]): Filter by repo. Use "*" for all repos.
        - output_format (str): "json" or "toon" for token-efficient format.
        - compact (bool): Minimal response fields.

        AROMA RERANKING PARAMETERS:
        - aroma_rerank (bool, default=True): Enable AROMA-style pruning/reranking.
          Improves precision by penalizing partial matches.
        - aroma_alpha (float, default=0.6): Weight for pruned similarity vs original.
          Higher values trust pruning more.

        RETURNS:
        {
            "ok": true,
            "results": [
                {
                    "path": "src/client.py",
                    "start_line": 89,
                    "end_line": 102,
                    "score": 0.78,
                    "language": "python",
                    "snippet": "for attempt in range(max_retries):..."
                }
            ],
            "total": 7,
            "query_mode": "code",         // or "description"
            "query_signature": "...",     // Internal: pattern signature used
            "detection": {                // Mode detection metadata
                "confidence": 0.95,
                "ast_validated": true,
                "signals": {"ast_parsed": 1.0, "nl_similarity": 0.42}
            }
        }

        CROSS-LANGUAGE EXAMPLE:
        # Find Go error handling similar to Python pattern
        pattern_search(
            query="if err != nil { return err }",
            language="go",
            target_languages=["python", "rust", "java"]
        )

        NOTES:
        - Pattern vectors must be indexed (PATTERN_VECTORS=1 during indexing)
        - Auto-detection uses AST parsing + NL embedder comparison
        - Code mode uses structural pattern matching
        - Description mode uses semantic search on pattern descriptions
        """
        return await _pattern_search_impl(
            query=query,
            language=language,
            limit=limit,
            min_score=min_score,
            include_snippet=include_snippet,
            context_lines=context_lines,
            hybrid=None,
            semantic_weight=None,
            collection=None,
            target_languages=target_languages,
            output_format=output_format,
            compact=compact,
            aroma_rerank=aroma_rerank,
            aroma_alpha=aroma_alpha,
            query_mode=query_mode,
            coerce_bool_fn=_coerce_bool,
            coerce_int_fn=_coerce_int,
            coerce_float_fn=lambda v, d: safe_float(v, default=d, logger=logger, context="pattern_search"),
        )


# ---------------------------------------------------------------------------
# Neo4j Graph Query - Advanced graph traversals (conditional on NEO4J_GRAPH=1)
# ---------------------------------------------------------------------------
_NEO4J_GRAPH_ENABLED = str(os.environ.get("NEO4J_GRAPH", "")).strip().lower() in {
    "1", "true", "yes", "on"
}

if _NEO4J_GRAPH_ENABLED:
    from scripts.mcp_impl.neo4j_graph import _neo4j_graph_query_impl

    @mcp.tool()
    async def neo4j_graph_query(
        query_type: Any = None,
        symbol: Any = None,
        depth: Any = None,
        limit: Any = None,
        repo: Any = None,
        language: Any = None,
        include_paths: Any = None,
        collection: Any = None,
        output_format: Any = None,
    ) -> Dict[str, Any]:
        """Advanced Neo4j graph queries for symbol relationships.

        Graph database queries enabled when NEO4J_GRAPH=1.

        Query types:
        - callers: Who calls this symbol? (depth 1)
        - callees: What does this symbol call? (depth 1)
        - transitive_callers: Multi-hop callers (up to depth)
        - transitive_callees: Multi-hop callees (up to depth)
        - impact: What would break if I change this? (reverse transitive)
        - dependencies: What does this depend on? (calls + imports)
        - cycles: Detect circular dependencies

        Key parameters:
        - query_type: str. Type of graph query (see above).
        - symbol: str. Symbol to analyze (required).
        - depth: int (default 1). Max traversal depth for transitive queries.
        - limit: int (default 50). Maximum results.
        - repo: str. Filter by repository.
        - include_paths: bool. Include full traversal paths in results.
        - collection: str. Graph collection scope (defaults to COLLECTION_NAME).
        - output_format: "json" (default) or "toon".

        Examples:
        - neo4j_graph_query(query_type="callers", symbol="authenticate")
        - neo4j_graph_query(query_type="impact", symbol="User", depth=3)
        - neo4j_graph_query(query_type="cycles", symbol="ServiceA")
        """
        _query_type = str(query_type).strip() if query_type else "callers"
        _symbol = str(symbol).strip() if symbol else None
        _depth = safe_int(depth, default=1, logger=logger, context="neo4j_graph.depth")
        _limit = safe_int(limit, default=50, logger=logger, context="neo4j_graph.limit")
        _repo = str(repo).strip() if repo else None
        _language = str(language).strip() if language else None
        _include_paths = _coerce_bool(include_paths, default=False)
        _collection = str(collection).strip() if collection else None
        _output_format = str(output_format).strip().lower() if output_format else "json"

        return await _neo4j_graph_query_impl(
            query_type=_query_type,
            symbol=_symbol,
            depth=_depth,
            limit=_limit,
            repo=_repo,
            language=_language,
            include_paths=_include_paths,
            collection=_collection,
            output_format=_output_format,
        )


_relax_var_kwarg_defaults()

if __name__ == "__main__":
    # Configure log level from environment
    import logging as _logging
    _log_level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
    _log_level = getattr(_logging, _log_level_str, _logging.INFO)
    _logging.getLogger().setLevel(_log_level)

    # Backend migration: detect file<->redis switch and migrate state if needed
    try:
        from scripts.workspace_state import detect_and_migrate_backend
        from pathlib import Path as _Path
        _ws_root = _Path(os.environ.get("WORKSPACE_PATH") or os.environ.get("WATCH_ROOT") or "/work")
        migrated = detect_and_migrate_backend(_ws_root)
        if migrated is not None:
            logger.info(f"[backend_migration] Migrated {migrated} items to new backend")
    except Exception as e:
        logger.warning(f"Backend migration check failed (continuing): {e}")

    # Startup logging with configuration info
    logger.info("=" * 60)
    logger.info("MCP Indexer Server starting...")
    logger.info("=" * 60)
    logger.info(f"  Host: {HOST}")
    logger.info(f"  Port: {PORT}")
    logger.info(f"  Log Level: {_log_level_str}")
    logger.info(f"  Qdrant URL: {os.environ.get('QDRANT_URL', 'not set')}")
    logger.info(f"  Collection: {os.environ.get('COLLECTION_NAME', 'codebase')}")
    logger.info(f"  Transport: {os.environ.get('FASTMCP_TRANSPORT', 'sse')}")
    logger.info(f"  Embedding Model: {os.environ.get('EMBEDDING_MODEL', 'BAAI/bge-base-en-v1.5')}")
    logger.info(f"  Embedding Provider: {os.environ.get('EMBEDDING_PROVIDER', 'fastembed')}")
    logger.info(f"  ReFRAG Decoder: {os.environ.get('REFRAG_DECODER', '1')}")
    logger.info(f"  Rerank Learning: {os.environ.get('RERANK_LEARNING', '1')}")
    logger.info(f"  Semantic Chunks: {os.environ.get('INDEX_SEMANTIC_CHUNKS', '1')}")
    logger.info(f"  Micro Chunks: {os.environ.get('INDEX_MICRO_CHUNKS', '1')}")
    logger.info(f"  Micro Chunk Tokens: {os.environ.get('MICRO_CHUNK_TOKENS', '128')}")
    logger.info(f"  Micro Chunk Stride: {os.environ.get('MICRO_CHUNK_STRIDE', '64')}")
    logger.info(f"  Max Micro Chunks/File: {os.environ.get('MAX_MICRO_CHUNKS_PER_FILE', '200')}")
    logger.info(f"  ReFRAG Mode: {os.environ.get('REFRAG_MODE', '0')}")
    logger.info(f"  ReFRAG Gate First: {os.environ.get('REFRAG_GATE_FIRST', '0')}")
    logger.info(f"  Lexical Vector Dim: {os.environ.get('LEX_VECTOR_DIM', '4096')}")
    logger.info(f"  Lexical Multi Hash: {os.environ.get('LEX_MULTI_HASH', '1')}")
    logger.info(f"  Lexical Bigrams: {os.environ.get('LEX_BIGRAMS', '0')}")
    logger.info(f"  Lexical Bigram Weight: {os.environ.get('LEX_BIGRAM_WEIGHT', '0.7')}")
    logger.info(f"  Lexical Sparse Mode: {os.environ.get('LEX_SPARSE_MODE', '0')}")
    logger.info(f"  Reranker Enabled: {os.environ.get('RERANKER_ENABLED', '0')}")
    logger.info(f"  Rerank Top N: {os.environ.get('RERANK_TOP_N', '20')}")
    logger.info(f"  Rerank Timeout MS: {os.environ.get('RERANK_TIMEOUT_MS', '500')}")
    logger.info(f"  Pattern Search: {'enabled' if _PATTERN_SEARCH_ENABLED else 'disabled (set PATTERN_VECTORS=1)'}")
    logger.info(f"  Neo4j Graph: {'enabled' if _NEO4J_GRAPH_ENABLED else 'disabled (set NEO4J_GRAPH=1)'}")
    logger.info("=" * 60)

    # Server warmup: async parallel loading of embedding + reranker models
    warmup_enabled = os.environ.get("SERVER_WARMUP_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
    if warmup_enabled:
        logger.info("Starting model warmup (embedding + reranker)...")
        try:
            from scripts.warm_start import warmup_all_models
            warmup_result = asyncio.run(warmup_all_models())
            logger.info(
                f"Warmup complete: embedding={warmup_result['embedding_ms']:.1f}ms, "
                f"reranker={warmup_result['reranker_ms']:.1f}ms, "
                f"total={warmup_result['total_ms']:.1f}ms"
            )
            # Set env var to signal readiness
            os.environ["SERVER_WARMED"] = "1"
        except Exception as e:
            logger.warning(f"Warmup failed (continuing anyway): {e}")
    else:
        logger.info("Model warmup disabled (SERVER_WARMUP_ENABLED=0)")

    # Legacy warmup fallback (kept for backward compat)
    try:
        if str(os.environ.get("EMBEDDING_WARMUP", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        } and not warmup_enabled:
            _ = _get_embedding_model(
                os.environ.get("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
            )
    except Exception as e:
        logger.debug(f"Suppressed exception: {e}")

    # Start lightweight /readyz health endpoint in background (best-effort)
    try:
        _start_readyz_server()
    except Exception as e:
        logger.debug(f"Suppressed exception: {e}")

    transport = os.environ.get("FASTMCP_TRANSPORT", "sse").strip().lower()
    # Enable stateless HTTP mode to avoid session handshake requirement
    stateless_http = str(os.environ.get("FASTMCP_STATELESS_HTTP", "1")).strip().lower() in {"1", "true", "yes", "on"}
    
    # Add auth header extraction middleware for HTTP transports
    if transport != "stdio":
        _add_auth_middleware()
    
    if transport == "stdio":
        # Run over stdio (for clients that don't support network transports)
        mcp.run(transport="stdio")
    elif transport in {"http", "streamable", "streamable_http", "streamable-http"}:
        # Streamable HTTP (recommended) — endpoint at /mcp (FastMCP default)
        try:
            mcp.settings.host = HOST
            mcp.settings.port = PORT
            # Set stateless mode via settings (not run kwarg)
            if stateless_http:
                mcp.settings.stateless_http = True
        except Exception as e:
            logger.debug(f"Suppressed exception setting config: {e}")
        # Use the correct FastMCP transport name
        try:
            logger.info(f"Starting streamable-http transport on {HOST}:{PORT} (stateless={stateless_http})")
            mcp.run(transport="streamable-http")
        except Exception as e:
            # Log the actual error instead of silently falling back
            logger.warning(f"streamable-http transport failed: {e}, falling back to SSE")
            mcp.settings.host = HOST
            mcp.settings.port = PORT
            mcp.run(transport="sse")
    else:
        # SSE (legacy) — endpoint at /sse
        mcp.settings.host = HOST
        mcp.settings.port = PORT
        mcp.run(transport="sse")
