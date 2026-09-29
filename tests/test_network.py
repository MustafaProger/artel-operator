"""Synthetic network routing checks; no portal requests or real credentials."""
import socket
import threading
from urllib.parse import urlsplit

import pytest

from operator_app import network


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.delenv("OPERATOR_NETWORK_INTERFACE", raising=False)


def test_ordinary_platform_needs_no_proxy(monkeypatch):
    monkeypatch.setattr(network.sys, "platform", "linux")
    with network.browser_proxy(allowed_hosts=("portal.example",)) as proxy:
        assert proxy == {}


def test_explicit_interface_fails_on_unsupported_platform(monkeypatch):
    monkeypatch.setattr(network.sys, "platform", "linux")
    monkeypatch.setenv("OPERATOR_NETWORK_INTERFACE", "en0")
    with pytest.raises(network.NetworkError, match="macOS"):
        network._network_interface()


def test_disabled_proxy_never_inspects_network(monkeypatch):
    monkeypatch.setenv("OPERATOR_NETWORK_INTERFACE", "off")
    monkeypatch.setattr(network, "_command", lambda *args: pytest.fail("Unexpected network inspection"))
    assert network._network_interface() is None


@pytest.mark.parametrize("state, addresses, expected", [
    (" en0 : flags : 0x5 (IPv4,DNS)", {"en0": True}, None),
    (" utun5 : flags : 0x5\n en0 : flags : 0x5", {"en0": True}, "en0"),
    (" utun5 : flags : 0x5\n en0 : flags : 0x5\n en4 : flags : 0x5",
     {"en0": False, "en4": True}, "en4"),
    (" utun5 : flags : 0x5", {}, None),
])
def test_selects_active_physical_interface_only_with_vpn(monkeypatch, state, addresses, expected):
    monkeypatch.setattr(network.sys, "platform", "darwin")
    monkeypatch.setattr(network, "_command", lambda *args: state)
    monkeypatch.setattr(network, "_ipv4_interface", lambda name: addresses.get(name, False))
    assert network._network_interface() == expected


@pytest.mark.parametrize("configured", ["utun5", "en0;echo secret", "en0"])
def test_explicit_interface_cannot_fallback_if_unavailable(monkeypatch, configured):
    monkeypatch.setattr(network.sys, "platform", "darwin")
    monkeypatch.setenv("OPERATOR_NETWORK_INTERFACE", configured)
    monkeypatch.setattr(network, "_ipv4_interface", lambda name: False)
    with pytest.raises(network.NetworkError, match="недоступен"):
        network._network_interface()


def test_explicit_interface_does_not_require_vpn(monkeypatch):
    monkeypatch.setattr(network.sys, "platform", "darwin")
    monkeypatch.setenv("OPERATOR_NETWORK_INTERFACE", "en4")
    monkeypatch.setattr(network, "_ipv4_interface", lambda name: name == "en4")
    assert network._network_interface() == "en4"


def test_binding_uses_darwin_interface_index(monkeypatch):
    calls = []
    monkeypatch.setattr(network.socket, "if_nametoindex", lambda name: 14)
    class FakeSocket:
        def setsockopt(self, *args):
            calls.append(args)
    network._bind_interface(FakeSocket(), "en4")
    assert calls == [(socket.IPPROTO_IP, getattr(socket, "IP_BOUND_IF", 25), 14)]


def test_binding_failure_does_not_start_server(monkeypatch):
    monkeypatch.setattr(network, "_network_interface", lambda: "en0")
    def fail(*args):
        raise network.NetworkError("Cannot bind")
    monkeypatch.setattr(network, "_bind_interface", fail)
    monkeypatch.setattr(network, "_TunnelServer", lambda *args: pytest.fail("Must fail before listening"))
    with pytest.raises(network.NetworkError):
        with network.browser_proxy(allowed_hosts=("portal.example",)):
            pytest.fail("Must not fall back to VPN")


@pytest.mark.parametrize("host, expected", [
    ("portal.example", True), ("assets.example.net", True),
    ("example.net", True), ("badexample.net", False),
    ("portal.example.attacker.test", False), ("other.example", False),
])
def test_domain_allowlist_has_label_boundaries(host, expected):
    assert network._allowed(host, ("portal.example", ".example.net")) == expected


@pytest.fixture
def proxy_environment(monkeypatch):
    servers = []
    original = network._TunnelServer
    def capture(*args):
        server = original(*args)
        servers.append(server)
        return server
    monkeypatch.setattr(network, "_network_interface", lambda: "en0")
    monkeypatch.setattr(network, "_bind_interface", lambda *args: None)
    monkeypatch.setattr(network, "_TunnelServer", capture)
    return servers


def connect(proxy):
    address = urlsplit(proxy["server"])
    return socket.create_connection((address.hostname, address.port), timeout=2)


@pytest.mark.parametrize("connect_request", [
    b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n",
    b"CONNECT portal.example:80 HTTP/1.1\r\n\r\n",
    b"GET https://portal.example/ HTTP/1.1\r\n\r\n",
    b"CONNECT user@portal.example:443 HTTP/1.1\r\n\r\n",
    b"CONNECT portal.example.evil.test:443 HTTP/1.1\r\n\r\n",
])
def test_proxy_rejects_unapproved_connect_without_opening_network(proxy_environment, monkeypatch, connect_request):
    monkeypatch.setattr(network, "_open_tunnel", lambda *args: pytest.fail("Unexpected outbound request"))
    with network.browser_proxy(allowed_hosts=("portal.example",)) as proxy:
        with connect(proxy) as client:
            client.sendall(connect_request)
            assert client.recv(4096).startswith(b"HTTP/1.1 403")


def test_failed_upstream_returns_sanitized_error_without_retry(proxy_environment, monkeypatch):
    attempts = []
    def fail(host, interface, server):
        attempts.append(host)
        raise OSError("private connection details")
    monkeypatch.setattr(network, "_open_tunnel", fail)
    with network.browser_proxy(allowed_hosts=("portal.example",)) as proxy:
        with connect(proxy) as client:
            client.sendall(b"CONNECT portal.example:443 HTTP/1.1\r\n\r\n")
            response = client.recv(4096)
            assert response.startswith(b"HTTP/1.1 502")
            assert b"private" not in response
    assert attempts == ["portal.example"]


def test_tunnel_relays_bytes_unchanged_and_closes_active_connections(proxy_environment, monkeypatch):
    peers, calls = [], []
    def synthetic_upstream(host, interface, server):
        upstream, peer = socket.socketpair()
        upstream.settimeout(2)
        peer.settimeout(2)
        peers.append(peer)
        calls.append((host, interface))
        server.track(upstream)
        return upstream
    monkeypatch.setattr(network, "_open_tunnel", synthetic_upstream)
    client = None
    try:
        with network.browser_proxy(allowed_hosts=("portal.example",)) as proxy:
            client = connect(proxy)
            # Payload resembles opaque TLS bytes, and is pipelined after CONNECT.
            payload = b"\x16\x03\x01opaque-client-tls-record"
            client.sendall(b"CONNECT portal.example:443 HTTP/1.1\r\nHost: portal.example\r\n\r\n" + payload)
            assert client.recv(4096) == b"HTTP/1.1 200 Connection Established\r\n\r\n"
            assert peers[0].recv(4096) == payload
            reply = b"\x16\x03\x03opaque-server-tls-record"
            peers[0].sendall(reply)
            assert client.recv(4096) == reply
            server = proxy_environment[0]
            listen_address = server.server_address
        assert calls == [("portal.example", "en0")]
        assert client.recv(1) == b""
        assert peers[0].recv(1) == b""
        assert not server.connections
        assert not any(t.name == "operator-network" and t.is_alive() for t in threading.enumerate())
        with pytest.raises(OSError):
            socket.create_connection(listen_address, timeout=0.2)
    finally:
        if client:
            client.close()
        for peer in peers:
            peer.close()


def test_context_exception_also_closes_listener(proxy_environment):
    with pytest.raises(RuntimeError, match="browser failure"):
        with network.browser_proxy(allowed_hosts=("portal.example",)):
            address = proxy_environment[0].server_address
            raise RuntimeError("browser failure")
    assert not proxy_environment[0].connections
    with pytest.raises(OSError):
        socket.create_connection(address, timeout=0.2)
