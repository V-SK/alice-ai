"""Pin the remote-gateway loopback proxy (routes/gateway_proxy_routes.py).

The WebView CSP is connect-src 'self', so gateway mode MUST route through these
loopback routes. The proxy's job is to be an HONEST, transparent transport:

  * GET /alice/gateway/models passes the gateway's body + status straight
    through (so the client's normalizeCatalog sees each tier's real status).
  * POST /alice/gateway/chat passes a 503 tier-status verdict VERBATIM — same
    status code, same JSON body, same Retry-After — so the client surfaces the
    gateway's own plaintext + an honest retry, never a fabricated completion.
  * A successful chat streams the SSE through unchanged (incl. alice_receipt).
  * A transport failure to the gateway becomes a 502 (distinct from the
    gateway's own 503) so the client keeps its static fallback catalog.
  * Only the Alice identity headers (X-Alice-Address / Authorization: Alice …)
    are forwarded upstream — never the local cookie/bearer.

httpx.AsyncClient is monkeypatched so no real network call is made.
"""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.gateway_proxy_routes import setup_gateway_proxy_routes


# ---------- a fake httpx.AsyncClient capturing the forwarded request ---------- #

class _FakeResponse:
    def __init__(self, status_code, content=b"", headers=None, stream_chunks=None):
        self.status_code = status_code
        self._content = content
        self.headers = headers or {}
        self._chunks = stream_chunks or []

    @property
    def content(self):
        return self._content

    async def aread(self):
        return self._content

    async def aiter_raw(self):
        for c in self._chunks:
            yield c

    async def aclose(self):
        pass


class _FakeClient:
    """Records the last request and returns a scripted response."""
    captured = {}
    script = None          # callable(method, url, headers, content) -> _FakeResponse
    raise_on_send = False

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def build_request(self, method, url, content=None, headers=None):
        return {"method": method, "url": url, "content": content, "headers": headers}

    async def get(self, url, headers=None):
        _FakeClient.captured = {"method": "GET", "url": url, "headers": headers or {}}
        if _FakeClient.raise_on_send:
            raise httpx.ConnectError("boom")
        return _FakeClient.script("GET", url, headers or {}, None)

    async def send(self, req, stream=False):
        _FakeClient.captured = {"method": req["method"], "url": req["url"],
                                "headers": req["headers"], "content": req["content"]}
        if _FakeClient.raise_on_send:
            raise httpx.ConnectError("boom")
        return _FakeClient.script(req["method"], req["url"], req["headers"], req["content"])

    async def aclose(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ALICE_GATEWAY_BASE", "https://gw.example.test")
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    _FakeClient.raise_on_send = False
    _FakeClient.captured = {}
    app = FastAPI()
    app.include_router(setup_gateway_proxy_routes())
    return TestClient(app)


# ----------------------------- /v1/models ------------------------------------ #

def test_models_passthrough_each_status(client):
    catalog = {"object": "list", "data": [
        {"id": "alice-lite-4b", "status": "ready", "parameter_billions": 4, "served": True},
        {"id": "alice-9b", "status": "loading", "parameter_billions": 9},
        {"id": "alice-27b", "status": "capacity_available", "parameter_billions": 27},
        {"id": "alice-flagship", "status": "no_capable_node", "parameter_billions": 744},
    ]}
    _FakeClient.script = lambda m, u, h, c: _FakeResponse(
        200, json.dumps(catalog).encode(), {"content-type": "application/json"})

    r = client.get("/alice/gateway/models",
                   headers={"X-Alice-Address": "alice1xyz", "Authorization": "Alice alice1xyz"})
    assert r.status_code == 200
    body = r.json()
    statuses = {m["id"]: m["status"] for m in body["data"]}
    assert statuses == {
        "alice-lite-4b": "ready", "alice-9b": "loading",
        "alice-27b": "capacity_available", "alice-flagship": "no_capable_node",
    }
    # forwarded to {base}/v1/models with the identity headers (only those)
    assert _FakeClient.captured["url"] == "https://gw.example.test/v1/models"
    assert _FakeClient.captured["headers"]["X-Alice-Address"] == "alice1xyz"
    assert _FakeClient.captured["headers"]["Authorization"] == "Alice alice1xyz"


def test_models_predeploy_shape_passes_through_untouched(client):
    # A pre-deploy gateway with no status fields: proxy is transparent; the
    # CLIENT decides back-compat (keeps its static fallback). Proxy just relays.
    catalog = {"object": "list", "data": [{"id": "alice", "name": "Alice"}]}
    _FakeClient.script = lambda m, u, h, c: _FakeResponse(
        200, json.dumps(catalog).encode(), {"content-type": "application/json"})
    r = client.get("/alice/gateway/models")
    assert r.status_code == 200
    assert r.json()["data"][0] == {"id": "alice", "name": "Alice"}


def test_models_transport_failure_is_502(client):
    _FakeClient.raise_on_send = True
    r = client.get("/alice/gateway/models")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "gateway_unreachable"


def test_local_cookie_is_not_forwarded_upstream(client):
    _FakeClient.script = lambda m, u, h, c: _FakeResponse(
        200, b'{"object":"list","data":[]}', {"content-type": "application/json"})
    client.get("/alice/gateway/models",
               headers={"Authorization": "Bearer local-token-should-not-leak",
                        "Cookie": "alice_local_token=secret"})
    fwd = _FakeClient.captured["headers"]
    # A non-"Alice " authorization is dropped; no cookie is forwarded.
    assert "Authorization" not in fwd
    assert "Cookie" not in fwd and "cookie" not in fwd


# ----------------------------- /v1/chat -------------------------------------- #

def test_chat_503_tier_loading_passed_through_verbatim(client):
    err_body = json.dumps({
        "error": {"code": "api_chat_model_tier_loading",
                  "message": "Spinning up the 27B tier; retry shortly."},
        "metadata": {"model_tier_status": "loading"},
    }).encode()
    _FakeClient.script = lambda m, u, h, c: _FakeResponse(
        503, err_body, {"content-type": "application/json", "retry-after": "12"})

    r = client.post("/alice/gateway/chat",
                    json={"model": "alice-27b", "messages": [{"role": "user", "content": "hi"}]})
    # VERBATIM: same status, same body, same Retry-After header.
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "12"
    body = r.json()
    assert body["error"]["code"] == "api_chat_model_tier_loading"
    assert "27B" in body["error"]["message"]
    # forwarded to the chat completions URL
    assert _FakeClient.captured["url"] == "https://gw.example.test/v1/chat/completions"


def test_chat_503_no_capable_node_passed_through(client):
    err_body = json.dumps({
        "error": {"code": "api_chat_model_tier_no_capable_node",
                  "message": "No capable node online can run this model."},
    }).encode()
    _FakeClient.script = lambda m, u, h, c: _FakeResponse(503, err_body, {"content-type": "application/json"})
    r = client.post("/alice/gateway/chat", json={"model": "alice-flagship", "messages": []})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "api_chat_model_tier_no_capable_node"


def test_chat_streams_sse_with_receipt(client):
    frames = [
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
        b'data: {"alice_receipt":{"spec_id":"spec:qwen3-4b@1","miner_id":"m1",'
        b'"output_token_ids_hash":"abc","paid_acu":0}}\n\n',
        b'data: [DONE]\n\n',
    ]
    _FakeClient.script = lambda m, u, h, c: _FakeResponse(
        200, b"", {"content-type": "text/event-stream"}, stream_chunks=frames)

    with client.stream("POST", "/alice/gateway/chat",
                       json={"model": "alice-lite-4b", "messages": [{"role": "user", "content": "hi"}]}) as r:
        assert r.status_code == 200
        text = "".join(chunk for chunk in r.iter_text())
    assert "Hello" in text and "world" in text
    assert "alice_receipt" in text and "spec:qwen3-4b@1" in text
    assert "[DONE]" in text


def test_chat_transport_failure_is_502(client):
    _FakeClient.raise_on_send = True
    r = client.post("/alice/gateway/chat", json={"model": "alice", "messages": []})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "gateway_unreachable"
