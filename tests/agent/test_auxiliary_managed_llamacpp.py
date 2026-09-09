"""Real config/state/HTTP routing before any main chat runtime exists."""

import contextvars
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest
import yaml


@pytest.fixture
def local_auxiliary(tmp_path, monkeypatch):
    import agent.auxiliary_client as aux
    from hermes_cli.local_runtime.supervisor import state_path

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(aux, "_RUNTIME_MAIN_CONTEXT", contextvars.ContextVar("isolated-test-main", default=None))
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/health"
            self._reply({"status": "ok"})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, self.headers.get("Authorization"), payload))
            self._reply({
                "id": "local-test", "object": "chat.completion", "created": 1,
                "model": payload["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "LOCAL_AUX_OK"}, "finish_reason": "stop"}],
            })

        def _reply(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "llamacpp", "default": "local-qwen", "base_url": ""},
    }), encoding="utf-8")
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps({"pid": os.getpid(), "base_url": base, "api_key": "local-test-key"}), encoding="utf-8")
    try:
        yield aux, base, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("provider", ["auto", "main", "llamacpp", "llama.cpp", "llama-cpp"])
def test_auxiliary_resolves_managed_model_without_chat_runtime(local_auxiliary, provider):
    aux, base, calls = local_auxiliary
    client, model = aux.resolve_provider_client(provider, task="vision")
    assert client is not None
    try:
        assert str(client.base_url).rstrip("/") == base
        assert model == "local-qwen"
        result = client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Check local routing"}])
        assert result.choices[0].message.content == "LOCAL_AUX_OK"
        assert calls[-1][1] == "Bearer local-test-key"
        assert calls[-1][2]["model"] == model
    finally:
        client.close()


def test_managed_identity_does_not_borrow_stale_cloud_environment(local_auxiliary, monkeypatch):
    aux, base, _ = local_auxiliary
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "cloud-key-must-not-be-forwarded")
    client, _ = aux.resolve_provider_client("llamacpp")
    try:
        assert str(client.base_url).rstrip("/") == base
        assert client.api_key == "local-test-key"
    finally:
        client.close()


def test_explicit_llamacpp_endpoint_and_key_win(local_auxiliary):
    aux, base, _ = local_auxiliary
    client, model = aux.resolve_provider_client(
        "llamacpp", "other-local-model", explicit_base_url=base + "/explicit",
        explicit_api_key="explicit-key",
    )
    try:
        assert str(client.base_url).rstrip("/") == base + "/explicit"
        assert client.api_key == "explicit-key"
        assert model == "other-local-model"
    finally:
        client.close()


def test_missing_local_endpoint_fails_without_cloud_substitution(local_auxiliary, monkeypatch):
    aux, _, _ = local_auxiliary
    monkeypatch.setattr("hermes_cli.local_runtime.endpoint.resolve_llamacpp_endpoint", lambda *a, **kw: None)
    assert aux.resolve_provider_client("llamacpp") == (None, None)


@pytest.mark.asyncio
async def test_async_auxiliary_uses_the_same_managed_identity(local_auxiliary):
    aux, base, calls = local_auxiliary
    client, model = aux.resolve_provider_client("auto", async_mode=True, task="compression")
    try:
        assert str(client.base_url).rstrip("/") == base
        result = await client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Check asynchronous routing"}])
        assert result.choices[0].message.content == "LOCAL_AUX_OK"
        assert calls[-1][1] == "Bearer local-test-key"
    finally:
        await client.close()


def test_named_custom_provider_with_alias_like_name_is_not_redirected(local_auxiliary, tmp_path):
    aux, base, _ = local_auxiliary
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "custom:llamacpp", "default": "named-model"},
        "custom_providers": [{"name": "llamacpp", "base_url": base + "/saved", "api_key": "saved-key"}],
    }), encoding="utf-8")
    client, model = aux.resolve_provider_client("custom:llamacpp")
    try:
        assert str(client.base_url).rstrip("/") == base + "/saved"
        assert client.api_key == "saved-key"
        assert model == "named-model"
    finally:
        client.close()
