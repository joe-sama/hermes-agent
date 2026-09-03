"""Regression tests for the install-E2E fake Internet proxy."""

import importlib.util
import ssl
import sys
from pathlib import Path

import pytest


PROXY_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sandbox" / "proxy.py"


def _load_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(PROXY_PATH),
            str(tmp_path / "fixtures"),
            str(tmp_path / "certs"),
            str(tmp_path / "real-ca.pem"),
        ],
    )
    spec = importlib.util.spec_from_file_location("sandbox_proxy_under_test", PROXY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RawSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _TLSContext:
    def __init__(self, outcomes):
        self._outcomes = iter(outcomes)
        self.hosts = []

    def wrap_socket(self, raw, *, server_hostname):
        self.hosts.append(server_hostname)
        outcome = next(self._outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Upstream:
    def __init__(self):
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def sendall(self, data):
        self.sent.append(data)


def test_https_setup_retries_unexpected_eof_before_returning_socket(
    tmp_path, monkeypatch, capsys
):
    proxy = _load_proxy(tmp_path, monkeypatch)
    first_raw = _RawSocket()
    second_raw = _RawSocket()
    raws = iter([first_raw, second_raw])
    connect_calls = []
    sleeps = []
    upstream = object()

    def connect(address, *, timeout):
        connect_calls.append((address, timeout))
        return next(raws)

    monkeypatch.setattr(proxy.socket, "create_connection", connect)
    monkeypatch.setattr(proxy.time, "sleep", sleeps.append)
    context = _TLSContext([ssl.SSLEOFError(8, "unexpected EOF"), upstream])

    opened = proxy.open_https_upstream("registry.npmjs.org", 443, context)

    assert opened is upstream
    assert first_raw.closed is True
    assert second_raw.closed is False
    assert connect_calls == [
        (("registry.npmjs.org", 443), proxy.UPSTREAM_TIMEOUT_SECONDS),
        (("registry.npmjs.org", 443), proxy.UPSTREAM_TIMEOUT_SECONDS),
    ]
    assert context.hosts == ["registry.npmjs.org", "registry.npmjs.org"]
    assert sleeps == [proxy.UPSTREAM_TLS_RETRY_DELAY_SECONDS]
    assert "retrying 1/3" in capsys.readouterr().err


def test_https_forward_sends_request_once_after_setup_retry(tmp_path, monkeypatch):
    proxy = _load_proxy(tmp_path, monkeypatch)
    raws = iter([_RawSocket(), _RawSocket()])
    upstream = _Upstream()
    context = _TLSContext([ssl.SSLEOFError(8, "unexpected EOF"), upstream])
    relays = []
    destination = object()
    request = b"GET /package HTTP/1.1\r\nHost: registry.npmjs.org\r\n\r\n"

    monkeypatch.setattr(
        proxy.socket,
        "create_connection",
        lambda _address, *, timeout: next(raws),
    )
    monkeypatch.setattr(proxy.ssl, "create_default_context", lambda **_kwargs: context)
    monkeypatch.setattr(proxy.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(proxy, "relay", lambda source, dest: relays.append((source, dest)))

    proxy.forward_https(destination, "registry.npmjs.org", 443, request)

    assert upstream.sent == [proxy.close_request(request)]
    assert relays == [(upstream, destination)]


@pytest.mark.parametrize("failure_stage", ["send", "relay"])
def test_https_forward_never_reopens_upstream_after_request_handling_starts(
    tmp_path, monkeypatch, failure_stage
):
    proxy = _load_proxy(tmp_path, monkeypatch)
    upstream = _Upstream()
    destination = object()
    request = b"POST /publish HTTP/1.1\r\nHost: registry.npmjs.org\r\n\r\npayload"
    opens = []

    monkeypatch.setattr(
        proxy.ssl,
        "create_default_context",
        lambda **_kwargs: object(),
    )

    def open_upstream(host, port, context):
        opens.append((host, port, context))
        return upstream

    monkeypatch.setattr(proxy, "open_https_upstream", open_upstream)

    if failure_stage == "send":
        def fail_send(data):
            upstream.sent.append(data)
            raise ConnectionResetError("send failed")

        monkeypatch.setattr(upstream, "sendall", fail_send)
        monkeypatch.setattr(
            proxy,
            "relay",
            lambda _source, _destination: pytest.fail("relay must not run"),
        )
    else:
        monkeypatch.setattr(
            proxy,
            "relay",
            lambda _source, _destination: (_ for _ in ()).throw(
                ConnectionResetError("relay failed")
            ),
        )

    with pytest.raises(ConnectionResetError):
        proxy.forward_https(destination, "registry.npmjs.org", 443, request)

    assert len(opens) == 1
    assert upstream.sent == [proxy.close_request(request)]


def test_https_setup_does_not_retry_certificate_failure(tmp_path, monkeypatch):
    proxy = _load_proxy(tmp_path, monkeypatch)
    raw = _RawSocket()
    connect_calls = []
    sleeps = []

    def connect(address, *, timeout):
        connect_calls.append((address, timeout))
        return raw

    monkeypatch.setattr(proxy.socket, "create_connection", connect)
    monkeypatch.setattr(proxy.time, "sleep", sleeps.append)
    context = _TLSContext(
        [ssl.SSLCertVerificationError(1, "certificate verify failed")]
    )

    with pytest.raises(ssl.SSLCertVerificationError):
        proxy.open_https_upstream("registry.npmjs.org", 443, context)

    assert raw.closed is True
    assert len(connect_calls) == 1
    assert sleeps == []


def test_https_setup_stops_after_bounded_retries(tmp_path, monkeypatch):
    proxy = _load_proxy(tmp_path, monkeypatch)
    raws = []
    sleeps = []

    def connect(_address, *, timeout):
        assert timeout == proxy.UPSTREAM_TIMEOUT_SECONDS
        raw = _RawSocket()
        raws.append(raw)
        return raw

    monkeypatch.setattr(proxy.socket, "create_connection", connect)
    monkeypatch.setattr(proxy.time, "sleep", sleeps.append)
    context = _TLSContext(
        [
            ssl.SSLEOFError(8, "unexpected EOF")
            for _ in range(proxy.UPSTREAM_TLS_SETUP_ATTEMPTS)
        ]
    )

    with pytest.raises(ssl.SSLEOFError):
        proxy.open_https_upstream("registry.npmjs.org", 443, context)

    assert len(raws) == proxy.UPSTREAM_TLS_SETUP_ATTEMPTS
    assert all(raw.closed for raw in raws)
    assert sleeps == [
        proxy.UPSTREAM_TLS_RETRY_DELAY_SECONDS,
        proxy.UPSTREAM_TLS_RETRY_DELAY_SECONDS * 2,
        proxy.UPSTREAM_TLS_RETRY_DELAY_SECONDS * 4,
    ]
