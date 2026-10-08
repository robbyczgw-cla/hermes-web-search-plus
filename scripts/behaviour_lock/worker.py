#!/usr/bin/env python3
"""Run one Web Search Plus checkout against a fixed query set, in isolation.

This worker is the measuring instrument for the 5.0 work. It loads a plugin
directory exactly the way Hermes does (``hermes_plugins.<slug>`` package),
calls the public tool handlers, and swaps ``http.client`` connections for a
transport that serves synthetic or recorded provider responses. It is
deliberately self-contained (stdlib only, no imports from the repository),
because it must drive *different* checkouts - the frozen v4.3.5 tag and the
current tree - with byte-identical tooling.

Run it as a separate process per checkout (``python -I worker.py ...``): the
plugin's flat sibling imports must never see modules from another checkout.

Transports
    synthetic  deterministic fake answers for every known provider endpoint
    replay     answers from a recorded corpus (``record`` output)
    record     real network, every exchange captured (needs real keys)

Modes
    route      routing decision per query, no HTTP at all
    tool       web_search_plus / web_extract_plus through the plugin handler
"""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import http.client
import importlib.util
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import types
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

PLUGIN_SLUG = "web_search_plus"
NS_PARENT = "hermes_plugins"
FAKE_PUBLIC_IP = "93.184.216.34"  # public-address fixture, never contacted

# Providers in the default auto pool; fake keys make them "configured".
AUTO_POOL_KEYS = {
    "YOU_API_KEY": "you",
    "SERPER_API_KEY": "serper",
    "EXA_API_KEY": "exa",
    "FIRECRAWL_API_KEY": "firecrawl",
    "TAVILY_API_KEY": "tavily",
    "LINKUP_API_KEY": "linkup",
    "PARALLEL_API_KEY": "parallel",
    "BRAVE_API_KEY": "brave",
}

HOST_PROVIDER = {
    "google.serper.dev": "serper",
    "scrape.serper.dev": "serper",
    "api.search.brave.com": "brave",
    "api.tavily.com": "tavily",
    "api.exa.ai": "exa",
    "ydc-index.io": "you",
    "api.ydc-index.io": "you",
    "api.linkup.so": "linkup",
    "api.parallel.ai": "parallel",
    "api.firecrawl.dev": "firecrawl",
    "api.serpbase.dev": "serpbase",
    "api.querit.ai": "querit",
}

_SECRET_KEY_RE = re.compile(r"(key|token|secret|auth|password|bearer)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Case context: which query the in-flight HTTP traffic belongs to.
# Research mode fans out on daemon threads, so this is process-global and the
# runner executes cases strictly one at a time.
# ---------------------------------------------------------------------------

class CaseContext:
    def __init__(self) -> None:
        self.case: Dict[str, Any] = {}
        self.exchanges: List[Dict[str, Any]] = []
        self.lock = threading.Lock()

    def start(self, case: Dict[str, Any]) -> None:
        with self.lock:
            self.case = case
            self.exchanges = []

    def log(self, exchange: Dict[str, Any]) -> None:
        with self.lock:
            self.exchanges.append(exchange)


CONTEXT = CaseContext()


# ---------------------------------------------------------------------------
# Request description and sanitizing (no secret ever leaves the process)
# ---------------------------------------------------------------------------

def _flatten(value: Any, prefix: str = "") -> Dict[str, str]:
    out: Dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                continue
            out.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.update(_flatten(item, f"{prefix}[{index}]"))
    else:
        out[prefix] = "" if value is None else str(value)
    return out


def describe_request(host: str, method: str, target: str, body: Optional[bytes]) -> Dict[str, Any]:
    parts = urlsplit(target)
    params = {k: v for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _SECRET_KEY_RE.search(k)}
    body_json: Any = None
    if body:
        try:
            body_json = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            body_json = None
    flat = dict(_flatten(params))
    if body_json is not None:
        flat.update(_flatten(body_json, "body"))
    return {
        "host": host,
        "provider": HOST_PROVIDER.get(host, "unknown"),
        "method": method,
        "path": parts.path or "/",
        "params": flat,
    }


def _param_distance(a: Dict[str, str], b: Dict[str, str]) -> int:
    keys = set(a) | set(b)
    return sum(1 for key in keys if a.get(key) != b.get(key))


# ---------------------------------------------------------------------------
# Synthetic provider answers
# ---------------------------------------------------------------------------

_SYNTH_DOMAINS = [
    "docs.example-docs.org", "www.example-news.com", "forum.example-forum.net",
    "shop.example-shop.de", "en.example-wiki.org", "blog.example-blog.io",
    "papers.example-arxiv.org", "m.example-mobile.com", "example-vendor.com",
    "security.example-cert.org",
]


def _digest(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest(), 16)


def _synthetic_docs(query: str) -> List[Dict[str, str]]:
    """Ten stable pseudo-documents per query, shared by every provider."""
    seed = _digest("docs", query)
    words = re.findall(r"\w+", query.lower()) or ["result"]
    slug = "-".join(words[:4])
    docs = []
    for index in range(10):
        domain = _SYNTH_DOMAINS[(seed >> (index * 3)) % len(_SYNTH_DOMAINS)]
        url = f"https://{domain}/{slug}-{index}"
        title = f"{query} - source {index} ({domain})"
        text = (
            f"{query}. Source {index} explains {' '.join(words)} in detail. "
            f"Paragraph about {words[index % len(words)]} with specifics and numbers {index * 7}. "
            f"A second paragraph mentions {words[-1]} and related background for readers."
        )
        docs.append({"url": url, "title": title, "text": text, "index": str(index)})
    return docs


def _synthetic_pick(query: str, provider: str, count: int) -> List[Dict[str, str]]:
    docs = _synthetic_docs(query)
    order = sorted(range(len(docs)), key=lambda i: _digest(provider, query, str(i)))
    picked = []
    for rank, index in enumerate(order[: max(1, count)]):
        doc = dict(docs[index])
        # Exercise URL normalization: tracking params and www./m. variants.
        variant = _digest("variant", provider, doc["url"]) % 4
        if variant == 1:
            doc["url"] += "?utm_source=search&utm_medium=web"
        elif variant == 2 and "://www." not in doc["url"]:
            doc["url"] = doc["url"].replace("://", "://www.", 1)
        doc["rank"] = str(rank)
        doc["date"] = f"2026-09-{(index % 27) + 1:02d}"
        picked.append(doc)
    return picked


def _synthetic_count(params: Dict[str, str], default: int = 5) -> int:
    for key in ("num", "count", "body.num", "body.numResults", "body.max_results", "body.limit",
                "body.advanced_settings.max_results", "max_results"):
        value = params.get(key)
        if value and str(value).isdigit():
            return max(1, min(int(value), 20))
    return default


def synthetic_answer(request: Dict[str, Any], query: str) -> Tuple[int, Dict[str, str], bytes, float]:
    host, path, params = request["host"], request["path"], request["params"]
    provider = request["provider"]
    count = _synthetic_count(params)
    latency_ms = 40.0 + (_digest("latency", provider) % 7) * 15.0
    docs = _synthetic_pick(query, provider, count)
    body: Any
    if host == "google.serper.dev":
        items = [{"title": d["title"], "link": d["url"], "snippet": d["text"][:160],
                  "date": d["date"], "position": int(d["rank"]) + 1} for d in docs]
        body = {"news": items} if path.endswith("/news") else {"organic": items, "relatedSearches": []}
    elif host == "api.search.brave.com":
        body = {"web": {"results": [{"title": d["title"], "url": d["url"], "description": d["text"][:180],
                                     "age": d["date"]} for d in docs]}}
    elif host == "api.tavily.com" and path.endswith("/extract"):
        urls = [v for k, v in sorted(params.items()) if k.startswith("body.urls[")]
        body = {"results": [{"url": u, "title": f"Extracted {u}", "raw_content": f"Full text of {u}. " * 6}
                            for u in urls], "failed_results": []}
    elif host == "api.tavily.com":
        body = {"results": [{"title": d["title"], "url": d["url"], "content": d["text"],
                             "score": round(0.9 - int(d["rank"]) * 0.1, 3)} for d in docs]}
    elif host == "api.exa.ai" and path.endswith("/contents"):
        urls = [v for k, v in sorted(params.items()) if k.startswith("body.urls[")]
        body = {"results": [{"url": u, "title": f"Extracted {u}", "text": f"Full text of {u}. " * 6} for u in urls]}
    elif host == "api.exa.ai":
        body = {"results": [{"title": d["title"], "url": d["url"], "text": d["text"] * 3,
                             "highlights": [d["text"][:120]], "score": round(0.8 - int(d["rank"]) * 0.05, 3),
                             "publishedDate": d["date"] + "T00:00:00.000Z"} for d in docs]}
    elif host in {"ydc-index.io", "api.ydc-index.io"} and path.endswith("/contents"):
        urls = [v for k, v in sorted(params.items()) if k.startswith("body.urls[")]
        body = [{"url": u, "title": f"Extracted {u}", "markdown": f"Full text of {u}. " * 6} for u in urls]
    elif host in {"ydc-index.io", "api.ydc-index.io"}:
        body = {"results": {"web": [{"title": d["title"], "url": d["url"], "description": d["text"][:100],
                                     "snippets": [d["text"][:160]], "page_age": d["date"]} for d in docs],
                            "news": []}, "metadata": {"search_uuid": "synthetic"}}
    elif host == "api.linkup.so" and path.endswith("/fetch"):
        body = {"markdown": f"Full text of {params.get('body.url', '')}. " * 6}
    elif host == "api.linkup.so":
        body = {"results": [{"name": d["title"], "url": d["url"], "content": d["text"], "type": "text"} for d in docs]}
    elif host == "api.parallel.ai" and path.endswith("/extract"):
        urls = [v for k, v in sorted(params.items()) if k.startswith("body.urls[")]
        body = {"results": [{"url": u, "title": f"Extracted {u}", "full_content": f"Full text of {u}. " * 6}
                            for u in urls], "errors": []}
    elif host == "api.parallel.ai":
        body = {"results": [{"title": d["title"], "url": d["url"], "excerpts": [d["text"]],
                             "publish_date": d["date"]} for d in docs], "search_id": "synthetic"}
    elif host == "api.firecrawl.dev" and path.endswith("/scrape"):
        url = params.get("body.url", "")
        body = {"success": True, "data": {"markdown": f"Full text of {url}. " * 6,
                                          "metadata": {"title": f"Extracted {url}", "sourceURL": url}}}
    elif host == "api.firecrawl.dev":
        body = {"success": True, "data": {"web": [{"title": d["title"], "url": d["url"],
                                                   "description": d["text"][:160],
                                                   "position": int(d["rank"]) + 1} for d in docs]}}
    else:
        return 404, {"Content-Type": "application/json"}, b'{"error":"synthetic: unknown endpoint"}', latency_ms
    return 200, {"Content-Type": "application/json"}, json.dumps(body).encode("utf-8"), latency_ms


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

class TransportError(Exception):
    pass


class Transport:
    latency_scale = 0.0

    def exchange(self, conn: "FakeConnection", method: str, target: str, body: Optional[bytes],
                 headers: Dict[str, str]) -> Tuple[int, Dict[str, str], bytes]:
        raise NotImplementedError


HANG_MS = 10_000_000.0  # "never answers": always beyond any client timeout


def _simulate_latency(latency_ms: float, timeout: Optional[float], scale: float) -> None:
    """Sleep the (scaled) latency; raise socket.timeout if it exceeds the timeout."""
    if latency_ms >= HANG_MS or (timeout is not None and latency_ms / 1000.0 > timeout):
        if scale and timeout is not None:
            time.sleep(timeout * scale)
        raise socket.timeout("timed out")
    if scale and latency_ms:
        time.sleep(latency_ms / 1000.0 * scale)


class SyntheticTransport(Transport):
    def __init__(self, latency_scale: float = 0.0, faults: Optional[Dict[str, Any]] = None) -> None:
        self.latency_scale = latency_scale
        self.faults = faults or {}

    def exchange(self, conn, method, target, body, headers):
        request = describe_request(conn.host, method, target, body)
        query = str(CONTEXT.case.get("query") or "")
        status, resp_headers, payload, latency_ms = synthetic_answer(request, query)
        fault = self.faults.get(request["provider"])
        if fault == "timeout":
            latency_ms = HANG_MS
        elif fault == "empty":
            payload = json.dumps({"results": [], "organic": [], "web": {"results": []},
                                  "data": {"web": []}}).encode("utf-8")
        elif fault == "http503":
            status, payload = 503, b'{"error":"unavailable"}'
        started = time.monotonic()
        try:
            _simulate_latency(latency_ms, conn.timeout, self.latency_scale)
        finally:
            CONTEXT.log({**request, "status": status, "latency_ms": latency_ms,
                         "wall_ms": round((time.monotonic() - started) * 1000, 3),
                         "error": "timeout" if fault == "timeout" else None})
        return status, resp_headers, payload


class ReplayTransport(Transport):
    def __init__(self, corpus_path: Path, latency_scale: float = 0.0,
                 faults: Optional[Dict[str, Any]] = None) -> None:
        self.latency_scale = latency_scale
        self.faults = faults or {}
        self.index: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        with corpus_path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                self.index.setdefault((row["case_id"], row["host"], row["path"]), []).append(row)

    def exchange(self, conn, method, target, body, headers):
        request = describe_request(conn.host, method, target, body)
        case_id = str(CONTEXT.case.get("id") or "")
        candidates = self.index.get((case_id, request["host"], request["path"]), [])
        fault = self.faults.get(request["provider"])
        started = time.monotonic()
        if not candidates:
            CONTEXT.log({**request, "status": None, "latency_ms": 0.0, "wall_ms": 0.0, "error": "unrecorded"})
            raise ConnectionRefusedError("replay: no recorded exchange")
        best = min(candidates, key=lambda row: _param_distance(row.get("params") or {}, request["params"]))
        latency_ms = float(best.get("latency_ms") or 0.0)
        error = best.get("error")
        if fault == "timeout":
            error = "timeout"
        try:
            if error == "timeout":
                # The provider did not answer within the recorded client timeout;
                # under replay it hangs until the *current* client timeout.
                _simulate_latency(HANG_MS, conn.timeout, self.latency_scale)
            elif error:
                _simulate_latency(latency_ms, conn.timeout, self.latency_scale)
                raise ConnectionResetError(f"replay: recorded {error}")
            else:
                _simulate_latency(latency_ms, conn.timeout, self.latency_scale)
        finally:
            CONTEXT.log({**request, "status": best.get("status"), "latency_ms": latency_ms,
                         "wall_ms": round((time.monotonic() - started) * 1000, 3),
                         "error": error, "param_distance": _param_distance(best.get("params") or {},
                                                                            request["params"])})
        status = int(best.get("status") or 200)
        body = best.get("body")
        requested = {v for k, v in request["params"].items() if k.startswith("body.urls[")}
        if requested and isinstance(body, dict):
            # A recorded extraction batch covers the union of several variants'
            # URLs; answer only for the URLs this request asked for.
            body = {key: [item for item in value if not isinstance(item, dict) or item.get("url") in requested]
                    if isinstance(value, list) else value for key, value in body.items()}
        payload = json.dumps(body).encode("utf-8") if "body" in best else base64.b64decode(
            best.get("body_b64") or b"")
        if fault == "empty":
            payload = json.dumps({"results": [], "organic": [], "web": {"results": []},
                                  "data": {"web": []}}).encode("utf-8")
        return status, {"Content-Type": "application/json"}, payload


_CLASS_SWAP_LOCK = threading.Lock()


class RecordTransport(Transport):
    """Pass through to the real network and append every exchange to a corpus."""

    def __init__(self, corpus_path: Path, real_http, real_https) -> None:
        self.corpus_path = corpus_path
        self.real_http, self.real_https = real_http, real_https
        self.write_lock = threading.Lock()

    def exchange(self, conn, method, target, body, headers):
        request = describe_request(conn.host, method, target, body)
        factory = self.real_https if conn.is_https else self.real_http
        kwargs = {"timeout": conn.timeout}
        if conn.is_https and conn.context is not None:
            kwargs["context"] = conn.context
        # HTTPSConnection.__init__ calls super(HTTPSConnection, self) through the
        # module global, which is patched; construct with the real names in place.
        with _CLASS_SWAP_LOCK:
            patched = (http.client.HTTPConnection, http.client.HTTPSConnection)
            http.client.HTTPConnection, http.client.HTTPSConnection = self.real_http, self.real_https
            try:
                real = factory(conn.host, conn.port, **kwargs)
            finally:
                http.client.HTTPConnection, http.client.HTTPSConnection = patched
        started = time.monotonic()
        error, status, resp_headers, raw = None, None, {}, b""
        try:
            real.request(method, target, body=body, headers=headers)
            response = real.getresponse()
            status = response.status
            resp_headers = {k: v for k, v in response.getheaders()}
            raw = response.read()
        except socket.timeout:
            error = "timeout"
        except OSError as exc:
            error = type(exc).__name__
        finally:
            real.close()
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        decoded = raw
        encoding = (resp_headers.get("Content-Encoding") or resp_headers.get("content-encoding") or "").lower()
        if encoding in {"gzip", "x-gzip"} or raw.startswith(b"\x1f\x8b"):
            decoded = gzip.decompress(raw)
        elif encoding == "deflate":
            decoded = zlib.decompress(raw)
        row = {"case_id": CONTEXT.case.get("id"), **request, "status": status,
               "latency_ms": latency_ms, "error": error, "recorded_at": int(time.time())}
        try:
            row["body"] = json.loads(decoded.decode("utf-8")) if decoded else None
        except (UnicodeDecodeError, ValueError):
            row["body_b64"] = base64.b64encode(decoded).decode("ascii")
        with self.write_lock, self.corpus_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        CONTEXT.log({**request, "status": status, "latency_ms": latency_ms, "wall_ms": latency_ms, "error": error})
        if error == "timeout":
            raise socket.timeout("timed out")
        if error:
            raise ConnectionResetError(error)
        clean = {k: v for k, v in resp_headers.items() if k.lower() not in {"content-encoding", "content-length",
                                                                             "transfer-encoding", "connection"}}
        return int(status), clean, decoded


class _FakeSocket:
    def __init__(self, data: bytes) -> None:
        self._file = io.BytesIO(data)

    def makefile(self, *_args, **_kwargs):
        return self._file

    def close(self) -> None:
        pass


class FakeConnection:
    """Stand-in for http.client.HTTP(S)Connection driven by a Transport."""

    transport: Transport = SyntheticTransport()
    is_https = False

    def __init__(self, host, port=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None,
                 *args, context=None, **kwargs) -> None:
        host_only, _, maybe_port = str(host).partition(":")
        self.host = host_only
        self.port = port or (int(maybe_port) if maybe_port.isdigit() else (443 if self.is_https else 80))
        self.timeout = None if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
        self.context = context
        self.sock = None
        self._response: Optional[http.client.HTTPResponse] = None
        self._method = "GET"

    # urllib plumbing
    def set_debuglevel(self, level) -> None:
        pass

    def set_tunnel(self, *args, **kwargs) -> None:
        raise OSError("worker: proxies are not supported")

    def connect(self) -> None:
        pass

    def close(self) -> None:
        self.sock = None

    def request(self, method, url, body=None, headers=None, *, encode_chunked=False) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        status, resp_headers, payload = self.transport.exchange(self, method, url, body, dict(headers or {}))
        reason = http.client.responses.get(status, "Status")
        head = [f"HTTP/1.1 {status} {reason}"]
        for key, value in resp_headers.items():
            head.append(f"{key}: {value}")
        head.append(f"Content-Length: {len(payload)}")
        head.append("Connection: close")
        raw = ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + payload
        response = http.client.HTTPResponse(_FakeSocket(raw), method=method)
        response.begin()
        self._response = response
        self._method = method

    def getresponse(self) -> http.client.HTTPResponse:
        if self._response is None:
            raise http.client.ResponseNotReady("worker: no request sent")
        response, self._response = self._response, None
        return response


class FakeHTTPSConnection(FakeConnection):
    is_https = True


def install_transport(transport: Transport) -> Callable[[], None]:
    real_http, real_https = http.client.HTTPConnection, http.client.HTTPSConnection
    if isinstance(transport, RecordTransport):
        transport.real_http, transport.real_https = real_http, real_https
    FakeConnection.transport = transport
    http.client.HTTPConnection = FakeConnection  # type: ignore[misc]
    http.client.HTTPSConnection = FakeHTTPSConnection  # type: ignore[misc]

    def restore() -> None:
        http.client.HTTPConnection, http.client.HTTPSConnection = real_http, real_https

    return restore


def install_fake_dns() -> None:
    """Resolve every host to a public fixture address (URL-safety checks stay on)."""
    real = socket.getaddrinfo

    def fake(host, port, *args, **kwargs):
        if host in {"localhost", "127.0.0.1", "::1"}:
            return real(host, port, *args, **kwargs)
        return real(FAKE_PUBLIC_IP, port, *args, **kwargs)

    socket.getaddrinfo = fake


# ---------------------------------------------------------------------------
# Plugin loading (mirrors hermes_cli.plugins_loader._load_directory_module)
# ---------------------------------------------------------------------------

class FakeContext:
    def __init__(self) -> None:
        self.tools: Dict[str, Dict[str, Any]] = {}
        self.commands: Dict[str, Any] = {}
        self.hooks: Dict[str, Any] = {}

    def register_tool(self, name, toolset=None, schema=None, handler=None, **kwargs):
        self.tools[name] = {"schema": schema, "handler": handler, **kwargs}

    def register_command(self, name, handler=None, **kwargs):
        self.commands[name] = handler

    def register_hook(self, name, handler):
        self.hooks[name] = handler


def load_plugin(plugin_dir: Path) -> Tuple[types.ModuleType, FakeContext]:
    if NS_PARENT not in sys.modules:
        ns_pkg = types.ModuleType(NS_PARENT)
        ns_pkg.__path__ = []  # type: ignore[attr-defined]
        ns_pkg.__package__ = NS_PARENT
        sys.modules[NS_PARENT] = ns_pkg
    module_name = f"{NS_PARENT}.{PLUGIN_SLUG}"
    init_file = plugin_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(module_name, init_file,
                                                  submodule_search_locations=[str(plugin_dir)])
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin from {plugin_dir}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    ctx = FakeContext()
    module.register(ctx)
    return module, ctx


def _find_router(plugin: types.ModuleType) -> Callable[[str, Dict[str, Any]], Dict[str, Any]]:
    """Locate the routing entry point across plugin layouts."""
    engine = None
    for loader_name in ("_load_search_module", "_load_engine"):
        loader = getattr(plugin, loader_name, None)
        if callable(loader):
            engine = loader()
            break
    candidates = [engine, getattr(plugin, "wsp_core", None)]
    for candidate in candidates:
        if candidate is None:
            continue
        route = getattr(candidate, "auto_route_provider", None)
        load_config = getattr(candidate, "load_config", None)
        if callable(route) and callable(load_config):
            return lambda query, _route=route, _load=load_config: _route(query, _load())
    raise RuntimeError("worker: no routing entry point found in plugin")


# ---------------------------------------------------------------------------
# Output normalization
# ---------------------------------------------------------------------------

VOLATILE_KEYS = {
    "cache_age_seconds", "request_id", "execution_id", "origin_execution_id", "latency", "latency_ms",
    "duration_ms", "elapsed_ms", "search_uuid", "search_id", "session_id", "recorded_at", "created_at",
    "entry_id", "started_at", "completed_at", "timestamp",
}


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in sorted(value.items()) if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, float):
        return round(value, 4)
    return value


def approx_tokens(text: str) -> int:
    """Stable tokenizer-free estimate: words plus punctuation runs (≈ cl100k within ~10%)."""
    return len(re.findall(r"\w+|[^\w\s]+", text))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _isolated_environment(work: Path, config: Dict[str, Any], keys: Dict[str, str]) -> None:
    hermes_home = work / "hermes-home"
    cache_dir = work / "cache"
    hermes_home.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    config_path = work / "config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    for name in list(os.environ):
        if name.endswith("_API_KEY") or name in {"SEARXNG_INSTANCE_URL", "DONSETCH_BIN", "WSP_FORCE_SUBPROCESS"}:
            del os.environ[name]
    os.environ.update({
        "HERMES_HOME": str(hermes_home),
        "WSP_CACHE_DIR": str(cache_dir),
        "WEB_SEARCH_PLUS_CONFIG": str(config_path),
        "WSP_HTTP_KEEPALIVE": "1",
        "NO_COLOR": "1",
    })
    os.environ.update(keys)


def _read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key in AUTO_POOL_KEYS and value:
            values[key] = value
    return values


def _tool_call(ctx: FakeContext, plugin: types.ModuleType, case: Dict[str, Any]) -> Dict[str, Any]:
    tool = case.get("tool", "web_search_plus")
    args = dict(case.get("args") or {})
    if tool == "web_search_plus":
        args.setdefault("query", case["query"])
    captured: Dict[str, Any] = {}
    formatter_name = "_format_results" if tool == "web_search_plus" else "_format_extract_results"
    original = getattr(plugin, formatter_name)

    def spy(data):
        captured["payload"] = copy.deepcopy(data)
        return original(data)

    setattr(plugin, formatter_name, spy)
    started = time.monotonic()
    try:
        output = ctx.tools[tool]["handler"](args)
        error = None
    except Exception as exc:  # the plugin must never raise to Hermes; record it if it does
        output, error = "", f"{type(exc).__name__}: {exc}"
    finally:
        setattr(plugin, formatter_name, original)
    wall_ms = (time.monotonic() - started) * 1000
    return {"output": output, "payload": captured.get("payload"), "wall_ms": wall_ms, "raised": error}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True, help="JSONL with id, query, optional tool/args")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=["route", "tool"], default="tool")
    parser.add_argument("--transport", choices=["synthetic", "replay", "record"], default="synthetic")
    parser.add_argument("--corpus", type=Path, help="replay input / record output JSONL")
    parser.add_argument("--latency-scale", type=float, default=0.0)
    parser.add_argument("--faults", default="{}", help='JSON, e.g. {"you": "timeout"}')
    parser.add_argument("--config", default="{}", help="JSON merged into the isolated config.json")
    parser.add_argument("--env-file", type=Path, help="record only: read real keys from this .env")
    parser.add_argument("--keys", default="", help="comma list of providers that get fake keys (default: auto pool)")
    parser.add_argument("--repeat", type=int, default=1, help="tool mode: run each case N times (latency)")
    args = parser.parse_args(argv)

    plugin_dir = args.plugin_dir.resolve()
    # Adaptive routing learns from earlier cases in the same run; switch it off so
    # every case sees the same router. Everything else stays at product defaults.
    config: Dict[str, Any] = {"auto_routing": {"adaptive_routing": False}}
    for section, values in json.loads(args.config).items():
        if isinstance(values, dict) and isinstance(config.get(section), dict):
            config[section] = {**config[section], **values}
        else:
            config[section] = values
    if args.transport == "record":
        if not args.env_file:
            parser.error("--transport record needs --env-file")
        keys = _read_env_file(args.env_file)
    else:
        wanted = [p.strip() for p in args.keys.split(",") if p.strip()] or list(AUTO_POOL_KEYS.values())
        keys = {env: "wsp-eval-fake-key-0123456789abcdef" for env, prov in AUTO_POOL_KEYS.items() if prov in wanted}

    work = Path(tempfile.mkdtemp(prefix="wsp-eval-"))
    _isolated_environment(work, config, keys)
    if args.transport != "record":
        install_fake_dns()
    faults = json.loads(args.faults)
    if args.transport == "synthetic":
        transport: Transport = SyntheticTransport(args.latency_scale, faults)
    elif args.transport == "replay":
        transport = ReplayTransport(args.corpus, args.latency_scale, faults)
    else:
        transport = RecordTransport(args.corpus, None, None)
    restore = install_transport(transport)

    import_started = time.monotonic()
    plugin, ctx = load_plugin(plugin_dir)
    import_ms = (time.monotonic() - import_started) * 1000
    router = _find_router(plugin) if args.mode == "route" else None

    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines() if line.strip()]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as out:
        out.write(json.dumps({"meta": {"plugin_dir": str(plugin_dir), "version": getattr(plugin, "__version__", None),
                                       "mode": args.mode, "transport": args.transport, "import_ms": import_ms,
                                       "cases": len(cases), "faults": faults}}) + "\n")
        for case in cases:
            CONTEXT.start(case)
            if args.mode == "route":
                decision = router(case["query"])
                row = {"id": case["id"], "decision": scrub(decision)}
            else:
                runs = [_tool_call(ctx, plugin, case) for _ in range(max(1, args.repeat))]
                first = runs[0]
                row = {
                    "id": case["id"],
                    "output": first["output"],
                    "payload": scrub(first["payload"]),
                    "raised": first["raised"],
                    "wall_ms": [round(run["wall_ms"], 3) for run in runs],
                    "approx_tokens": approx_tokens(first["output"]),
                    "chars": len(first["output"]),
                    "http": [{k: v for k, v in ex.items() if k != "params"} for ex in CONTEXT.exchanges],
                }
            out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    restore()
    return 0


if __name__ == "__main__":
    sys.exit(main())
