"""Network fault injection: only fake sockets, socketpairs and loopback listeners.

No real DNS, portal, interface binding, VPN or browser is used. Generated cases
have fixed seeds; repeated contexts are counted separately from unique cases.
"""
from concurrent.futures import ThreadPoolExecutor
import random
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit

import pytest

from operator_app import network


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.delenv("OPERATOR_NETWORK_INTERFACE", raising=False)


class Client:
    def __init__(self, chunks=()):
        self.chunks = iter(chunks)
        self.sent = bytearray()
        self.timeouts = []
        self.closed = False

    def recv(self, size):
        chunk = next(self.chunks, b"")
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def sendall(self, data):
        self.sent.extend(data)

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


class Server:
    track = network._TunnelServer.track
    release = network._TunnelServer.release
    close_connections = network._TunnelServer.close_connections

    def __init__(self, patterns=(".example.test",)):
        self.interface = "en0"
        self.allowed_hosts = patterns
        self.stopping = threading.Event()
        self.connections = set()
        self.connection_lock = threading.Lock()


def handle(client, server=None):
    server = server or Server()
    network._TunnelHandler(client, ("127.0.0.1", 1), server)
    assert not server.connections
    assert client.closed
    return bytes(client.sent)


@pytest.fixture
def local_proxy(monkeypatch):
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


def receive_exact(connection, size):
    result = bytearray()
    while len(result) < size:
        part = connection.recv(size - len(result))
        assert part, "Tunnel truncated the opaque stream"
        result.extend(part)
    return bytes(result)


# These four regressions reproduce deficiencies in the initial tunnel.
def test_listener_closed_if_serve_thread_cannot_start(local_proxy, monkeypatch):
    def unavailable(self):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(network.threading.Thread, "start", unavailable)
    try:
        with pytest.raises(RuntimeError, match="cannot start"):
            with network.browser_proxy(allowed_hosts=("portal.example",)):
                pytest.fail("A failed server must not yield proxy settings")
        assert local_proxy[0].socket.fileno() == -1
    finally:
        # Also prevent the intentionally failing pre-fix test from leaking.
        local_proxy[0].server_close()


@pytest.mark.parametrize("host", [
    ".example.test", "a..example.test", "-a.example.test", "a-.example.test",
    "portal.example.test..", "a" * 64 + ".example.test",
    ".".join(["a" * 63] * 4) + ".example.test",
])
def test_malformed_dns_name_never_reaches_resolver(monkeypatch, host):
    calls = []

    def forbidden(*args):
        calls.append(args[0])
        raise OSError("Must not resolve malformed names")

    monkeypatch.setattr(network, "_open_tunnel", forbidden)
    request = f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode()
    response = handle(Client([request]))
    assert response.startswith(b"HTTP/1.1 403")
    assert calls == []


def test_dripped_header_has_total_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(network.time, "monotonic", lambda: now[0])
    reads = []

    class DrippingClient(Client):
        def recv(self, size):
            reads.append(now[0])
            now[0] += 3
            return next(self.chunks, b"")

    client = DrippingClient([b"CON", b"NECT ", b"example.test:443 HTTP/1.1\r\n\r\n"])
    calls = []

    def forbidden(*args):
        calls.append(args)
        raise OSError("Must stop reading at total deadline")

    monkeypatch.setattr(network, "_open_tunnel", forbidden)
    handle(client)
    assert len(reads) <= 2
    assert not calls
    assert client.timeouts[-1] <= 2


def test_header_budget_does_not_include_pipelined_tls(monkeypatch):
    prefix = b"CONNECT example.test:443 HTTP/1.1\r\nX-Padding: "
    header = prefix + b"a" * (16370 - len(prefix) - 4) + b"\r\n\r\n"
    opaque = b"\x16\x03\x03" + b"x" * 100
    request = header + opaque
    client = Client([request[i:i + 4000] for i in range(0, len(request), 4000)])
    upstream = Client()

    def open_fake(host, interface, server):
        server.track(upstream)
        return upstream

    monkeypatch.setattr(network, "_open_tunnel", open_fake)
    monkeypatch.setattr(network.select, "select", lambda *args: ([upstream], [], []))
    assert handle(client).startswith(b"HTTP/1.1 200")
    assert upstream.sent == opaque


@pytest.mark.parametrize("configured", ["off", "OFF", " none ", "disabled", "DiSaBlEd"])
@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_disabled_modes_do_not_inspect_interfaces(monkeypatch, configured, platform):
    monkeypatch.setenv("OPERATOR_NETWORK_INTERFACE", configured)
    monkeypatch.setattr(network.sys, "platform", platform)
    monkeypatch.setattr(network, "_command", lambda *args: pytest.fail("Network inspection"))
    assert network._network_interface() is None


@pytest.mark.parametrize("configured", ["", "auto", "AUTO", "  auto  "])
@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_automatic_on_other_platforms_is_ordinary_routing(monkeypatch, configured, platform):
    monkeypatch.setenv("OPERATOR_NETWORK_INTERFACE", configured)
    monkeypatch.setattr(network.sys, "platform", platform)
    monkeypatch.setattr(network, "_command", lambda *args: pytest.fail("Network inspection"))
    assert network._network_interface() is None


@pytest.mark.parametrize("configured", [
    "utun0", "en", "en-1", "en1000", "en0;echo token", "en0\nen1", "lo0", "EN0", "en٠",
])
def test_invalid_explicit_interface_fails_before_commands(monkeypatch, configured):
    monkeypatch.setattr(network.sys, "platform", "darwin")
    monkeypatch.setenv("OPERATOR_NETWORK_INTERFACE", configured)
    monkeypatch.setattr(network, "_ipv4_interface", lambda *args: pytest.fail("Invalid name inspected"))
    with pytest.raises(network.NetworkError, match="недоступен"):
        network._network_interface()


@pytest.mark.parametrize("state, available, expected", [
    ("", set(), None),
    ("en0 : flags : x\nen1 : flags : x", {"en0", "en1"}, None),
    ("utun8 : flags : x\nen0 : flags : x", set(), None),
    ("utun8 : flags : x\nen0 : flags : x\nen9 : flags : x", {"en9"}, "en9"),
    ("utun8 : flags : x\nen9 : flags : x\nen0 : flags : x", {"en9", "en0"}, "en9"),
    ("utun8 : flags : x\nen1000 : flags : x\nbridge0 : flags : x", {"en1000", "bridge0"}, None),
    ("IPv4 network interface information\n  en12 : flags : x\n  utun1 : flags : x", {"en12"}, "en12"),
])
def test_vpn_selection_uses_first_valid_physical_interface(monkeypatch, state, available, expected):
    monkeypatch.setattr(network.sys, "platform", "darwin")
    monkeypatch.setattr(network, "_command", lambda *args: state)
    monkeypatch.setattr(network, "_ipv4_interface", lambda name: name in available)
    assert network._network_interface() == expected


@pytest.mark.parametrize("value, expected", [
    ("192.168.50.8", True), ("10.2.3.4\n", True), ("172.17.0.1", True),
    ("0.0.0.0", False), ("127.0.0.1", False), ("169.254.1.2", False),
    ("", False), ("::1", False), ("192.168.1.4/24", False), ("permission denied", False),
])
def test_ipv4_probe_validation(monkeypatch, value, expected):
    calls = []

    def command(*args):
        calls.append(args)
        return value

    monkeypatch.setattr(network, "_command", command)
    assert network._ipv4_interface("en4") is expected
    assert calls == [("/usr/sbin/ipconfig", "getifaddr", "en4")]


@pytest.mark.parametrize("outcome", ["nonzero", "missing", "timeout", "success"])
def test_system_probe_has_timeout_and_no_shell(monkeypatch, outcome):
    def run(args, **kwargs):
        assert args == ("/usr/sbin/scutil", "--nwi")
        assert kwargs == {"capture_output": True, "text": True, "timeout": 3, "check": False}
        if outcome == "missing":
            raise FileNotFoundError("synthetic")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(args, 3)
        return subprocess.CompletedProcess(args, int(outcome == "nonzero"), "state", "private")

    monkeypatch.setattr(network.subprocess, "run", run)
    assert network._command("/usr/sbin/scutil", "--nwi") == ("state" if outcome == "success" else "")


@pytest.mark.parametrize("patterns", [
    (), ("*",), ("*.example.test",), ("https://example.test",), ("example.test:443",),
    ("example.test/private",), ("example.test\r\nX:secret",), ("-a.test",),
    ("a-.test",), ("a..test",), ("example.test..",), ("a" * 64 + ".test",),
])
def test_invalid_allowlist_fails_before_socket(monkeypatch, patterns):
    monkeypatch.setattr(network, "_network_interface", lambda: "en0")
    monkeypatch.setattr(network.socket, "socket", lambda *args: pytest.fail("Opened socket for invalid allowlist"))
    with pytest.raises(network.NetworkError, match="домены"):
        with network.browser_proxy(allowed_hosts=patterns):
            pytest.fail("Invalid allowlist accepted")


def generated_domain_cases():
    rng = random.Random(20260929)
    cases = []
    for index in range(24):
        label = "n" + "".join(rng.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=12))
        cases.extend([
            (f"{label}.example.test", True),
            (f"{label}example.test", False),
            (f"example.test.{label}.test", False),
            (f"{label}.other.test", False),
        ])
    return cases


@pytest.mark.parametrize("host, allowed", generated_domain_cases())
def test_seeded_domain_boundary_cases(monkeypatch, host, allowed):
    calls = []

    def open_fake(host, interface, server):
        calls.append(host)
        raise OSError("Simulated unreachable permitted host")

    monkeypatch.setattr(network, "_open_tunnel", open_fake)
    response = handle(Client([f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode()]))
    assert response.startswith(b"HTTP/1.1 502" if allowed else b"HTTP/1.1 403")
    assert calls == ([host] if allowed else [])


@pytest.mark.parametrize("wire_request", [
    b"GET https://example.test/ HTTP/1.1", b"POST example.test:443 HTTP/1.1",
    b"CONNECT example.test:80 HTTP/1.1", b"CONNECT example.test:444 HTTP/1.1",
    b"CONNECT example.test:0443 HTTP/1.1", b"CONNECT example.test HTTP/1.1",
    b"CONNECT user:secret@example.test:443 HTTP/1.1", b"CONNECT example.test:443/path HTTP/1.1",
    b"CONNECT [::1]:443 HTTP/1.1", b"CONNECT 127.0.0.1:443 HTTP/1.1",
    b"CONNECT example.test%00.attacker:443 HTTP/1.1", b"CONNECT example.test\x00:443 HTTP/1.1",
    b"CONNECT example.test:443 HTTP/2.0", b"connect example.test:443 HTTP/1.1",
    b"CONNECT  example.test:443 HTTP/1.1", b"CONNECT\texample.test:443 HTTP/1.1",
    b"CONNECT example.test:443 HTTP/1.1 x", b"CONNECT ex\xffample.test:443 HTTP/1.1",
])
def test_invalid_connect_never_opens_upstream(monkeypatch, wire_request):
    monkeypatch.setattr(network, "_open_tunnel", lambda *args: pytest.fail("Unapproved upstream"))
    assert handle(Client([wire_request + b"\r\n\r\n"])).startswith(b"HTTP/1.1 403")


@pytest.mark.parametrize("host", ["EXAMPLE.TEST", "Example.Test.", "a.Example.Test."])
@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_valid_connect_canonicalization(monkeypatch, host, version):
    calls = []

    def open_fake(host, interface, server):
        calls.append(host)
        raise OSError("synthetic")

    monkeypatch.setattr(network, "_open_tunnel", open_fake)
    request = f"CONNECT {host}:443 HTTP/{version}\r\nHost: attacker.test\r\n\r\n".encode()
    assert handle(Client([request])).startswith(b"HTTP/1.1 502")
    assert calls == [host.lower().removesuffix(".")]


@pytest.mark.parametrize("chunks", [[b"CON", b""], [TimeoutError("secret")],
                                        [ConnectionResetError("secret")], [b"a" * 4096] * 5])
def test_header_eof_timeout_reset_and_size_release_client(monkeypatch, chunks, capsys):
    monkeypatch.setattr(network, "_open_tunnel", lambda *args: pytest.fail("Unapproved upstream"))
    response = handle(Client(chunks))
    assert b"secret" not in response
    assert capsys.readouterr() == ("", "")


class Outbound(Client):
    def __init__(self, outcome=None, on_connect=None):
        super().__init__()
        self.outcome = outcome
        self.on_connect = on_connect
        self.addresses = []

    def connect(self, address):
        self.addresses.append(address)
        if self.on_connect:
            self.on_connect()
        if self.outcome:
            raise self.outcome


def fake_outbound(monkeypatch, outcomes, *, elapsed=None):
    sockets, binds, dns = [], [], []

    def resolve(*args):
        dns.append(args)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"192.0.2.{i}", 443)) for i in range(1, 7)]

    def create(*args):
        assert args == (socket.AF_INET, socket.SOCK_STREAM, 6)
        index = len(sockets)
        sock = Outbound(outcomes[index], elapsed)
        sockets.append(sock)
        return sock

    monkeypatch.setattr(network.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(network.socket, "socket", create)
    monkeypatch.setattr(network, "_bind_interface", lambda conn, interface: binds.append((conn, interface)))
    return sockets, binds, dns


def test_connect_failover_happens_before_any_payload(monkeypatch):
    sockets, binds, dns = fake_outbound(monkeypatch, [ConnectionRefusedError(), TimeoutError(), None])
    server = Server()
    result = network._open_tunnel("example.test", "en2", server)
    assert dns == [("example.test", 443, socket.AF_INET, socket.SOCK_STREAM)]
    assert len(sockets) == 3
    assert all(sock.closed and not sock.sent for sock in sockets[:2])
    assert result is sockets[2] and server.connections == {result}
    assert [item[1] for item in binds] == ["en2"] * 3
    assert all(0 < sock.timeouts[0] <= 15 for sock in sockets)
    assert result.timeouts[-1] == 15
    server.close_connections()


def test_connect_attempts_are_bounded_by_four_addresses(monkeypatch):
    sockets, _, _ = fake_outbound(monkeypatch, [OSError("private")] * 4)
    server = Server()
    with pytest.raises(network.NetworkError, match="не ответил") as error:
        network._open_tunnel("example.test", "en0", server)
    assert "private" not in str(error.value)
    assert len(sockets) == 4 and all(sock.closed for sock in sockets)
    assert not server.connections


def test_connect_deadline_is_shared_across_addresses(monkeypatch):
    now = [0]
    monkeypatch.setattr(network.time, "monotonic", lambda: now[0])

    def elapsed():
        now[0] += 8

    sockets, _, _ = fake_outbound(monkeypatch, [TimeoutError()] * 4, elapsed=elapsed)
    server = Server()
    with pytest.raises(network.NetworkError):
        network._open_tunnel("example.test", "en0", server)
    assert len(sockets) == 2
    assert [sock.timeouts[0] for sock in sockets] == [15, 7]
    assert not server.connections


@pytest.mark.parametrize("failure", [socket.gaierror("private DNS"), TimeoutError("private DNS")])
def test_dns_failure_has_safe_message_and_never_creates_socket(monkeypatch, failure):
    def resolve(*args):
        raise failure

    monkeypatch.setattr(network.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(network.socket, "socket", lambda *args: pytest.fail("Socket opened after DNS failure"))
    with pytest.raises(network.NetworkError, match="определить адрес") as error:
        network._open_tunnel("example.test", "en0", Server())
    assert "private" not in str(error.value)


def test_bind_failure_closes_socket_without_trying_another_address(monkeypatch):
    sockets, _, _ = fake_outbound(monkeypatch, [None])

    def bind(*args):
        raise network.NetworkError("synthetic bind failure")

    monkeypatch.setattr(network, "_bind_interface", bind)
    server = Server()
    with pytest.raises(network.NetworkError, match="bind"):
        network._open_tunnel("example.test", "en0", server)
    assert len(sockets) == 1 and sockets[0].closed
    assert not sockets[0].addresses and not server.connections


@pytest.mark.parametrize("stage", ["index", "setsockopt"])
def test_real_binding_path_sanitizes_os_failures(monkeypatch, stage):
    def index(name):
        assert name == "en2"
        if stage == "index":
            raise OSError("private interface details")
        return 4

    class Socket:
        def setsockopt(self, *args):
            assert args == (socket.IPPROTO_IP, getattr(socket, "IP_BOUND_IF", 25), 4)
            raise OSError("private interface details")

    monkeypatch.setattr(network.socket, "if_nametoindex", index)
    with pytest.raises(network.NetworkError, match="привязать") as error:
        network._bind_interface(Socket(), "en2")
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("resolved", [[], "expired"])
def test_empty_dns_or_exhausted_dns_budget_never_connects(monkeypatch, resolved):
    now = [0]
    monkeypatch.setattr(network.time, "monotonic", lambda: now[0])

    def resolve(*args):
        if resolved == "expired":
            now[0] = 16
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]
        return resolved

    monkeypatch.setattr(network.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(network.socket, "socket", lambda *args: pytest.fail("Connect after empty/late DNS"))
    with pytest.raises(network.NetworkError, match="не ответил"):
        network._open_tunnel("example.test", "en0", Server())


def test_socket_creation_failure_returns_safe_502(monkeypatch, capsys):
    monkeypatch.setattr(network.socket, "getaddrinfo", lambda *args: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))])

    def create(*args):
        raise OSError("Too many files: private path")

    monkeypatch.setattr(network.socket, "socket", create)
    response = handle(Client([b"CONNECT example.test:443 HTTP/1.1\r\n\r\n"]))
    assert response == b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n"
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("failure_stage", ["send_pending", "read_upstream", "send_client", "select"])
def test_established_tunnel_errors_close_both_peers_without_replay(monkeypatch, failure_stage, capsys):
    client = Client([b"CONNECT example.test:443 HTTP/1.1\r\n\r\n\x16\x03opaque"])
    upstream = Client([b"reply"])
    calls = []

    def fail(*args):
        raise OSError("synthetic private TLS/session contents")

    def open_fake(host, interface, server):
        calls.append(host)
        server.track(upstream)
        return upstream

    monkeypatch.setattr(network, "_open_tunnel", open_fake)
    monkeypatch.setattr(network.select, "select", lambda *args: ([upstream], [], []))
    if failure_stage == "send_pending":
        monkeypatch.setattr(upstream, "sendall", fail)
    elif failure_stage == "read_upstream":
        monkeypatch.setattr(upstream, "recv", fail)
    elif failure_stage == "select":
        monkeypatch.setattr(network.select, "select", fail)
    else:
        original_send = client.sendall

        def fail_after_connect(data):
            if not data.startswith(b"HTTP/1.1 200"):
                fail()
            original_send(data)

        monkeypatch.setattr(client, "sendall", fail_after_connect)
    response = handle(client)
    assert response == b"HTTP/1.1 200 Connection Established\r\n\r\n"
    assert upstream.closed and calls == ["example.test"]
    assert capsys.readouterr() == ("", "")


def test_stop_during_connect_does_not_retry(monkeypatch):
    server = Server()
    sockets, _, _ = fake_outbound(monkeypatch, [ConnectionAbortedError()], elapsed=server.close_connections)
    with pytest.raises(network.NetworkError):
        network._open_tunnel("example.test", "en0", server)
    assert len(sockets) == 1 and sockets[0].closed
    assert not server.connections


def test_track_after_shutdown_closes_rejected_socket():
    server = Server()
    server.close_connections()
    connection = Client()
    with pytest.raises(OSError, match="stopped"):
        server.track(connection)
    assert connection.closed and not server.connections


@pytest.mark.parametrize("seed", [20260929, 390124])
def test_concurrent_tracking_release_and_shutdown(seed):
    rng = random.Random(seed)
    server = Server()
    pairs = [socket.socketpair() for _ in range(40)]
    gate = threading.Barrier(9)
    groups = [pairs[index::8] for index in range(8)]
    rng.shuffle(groups)

    def use_sockets(group):
        gate.wait(timeout=3)
        for connection, peer in group:
            try:
                server.track(connection)
            except OSError:
                pass
            finally:
                server.release(connection)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(use_sockets, group) for group in groups]
            gate.wait(timeout=3)
            server.close_connections()
            for future in futures:
                future.result(timeout=3)
        assert not server.connections
        assert all(connection.fileno() == -1 for connection, _ in pairs)
        for _, peer in pairs:
            peer.settimeout(1)
            assert peer.recv(1) == b""
    finally:
        for connection, peer in pairs:
            connection.close()
            peer.close()


@pytest.mark.parametrize("seed", [20260929, 390124])
def test_repeated_contexts_preserve_stream_and_release_resources(local_proxy, monkeypatch, seed):
    rng = random.Random(seed)
    peers = []
    initial_threads = set(threading.enumerate())

    def open_fake(host, interface, server):
        upstream, peer = socket.socketpair()
        upstream.settimeout(2)
        peer.settimeout(2)
        peers.append(peer)
        server.track(upstream)
        return upstream

    monkeypatch.setattr(network, "_open_tunnel", open_fake)
    try:
        for index in range(12):
            with network.browser_proxy(allowed_hosts=("example.test",)) as proxy:
                with connect(proxy) as client:
                    # Several TCP fragments, including a TLS-shaped prefix, must
                    # remain opaque. An HTTP-looking payload is likewise opaque.
                    payload = b"\x16\x03\x03" + rng.randbytes(rng.randint(1, 4000))
                    header = b"CONNECT example.test:443 HTTP/1.1\r\n\r\n"
                    client.sendall(header[:9])
                    client.sendall(header[9:] + payload)
                    response = b"HTTP/1.1 200 Connection Established\r\n\r\n"
                    assert receive_exact(client, len(response)) == response
                    assert receive_exact(peers[-1], len(payload)) == payload
                    reply = rng.randbytes(rng.randint(1, 2000))
                    peers[-1].sendall(reply)
                    assert receive_exact(client, len(reply)) == reply
                    if index % 2:
                        # Peer EOF must close the client without reconnect/replay.
                        peers[-1].shutdown(socket.SHUT_WR)
                        assert client.recv(1) == b""
            assert local_proxy[-1].socket.fileno() == -1
            assert not local_proxy[-1].connections
            peers[-1].close()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            added = set(threading.enumerate()) - initial_threads
            if not any(t.name == "operator-network" or "process_request_thread" in t.name for t in added):
                break
            time.sleep(0.01)
        assert not any(t.name == "operator-network" or "process_request_thread" in t.name
                       for t in set(threading.enumerate()) - initial_threads)
        assert len(peers) == 12
    finally:
        for peer in peers:
            peer.close()


def test_shutdown_closes_incomplete_headers_and_active_tunnel(local_proxy, monkeypatch):
    peers = []

    def open_fake(host, interface, server):
        upstream, peer = socket.socketpair()
        peer.settimeout(2)
        peers.append(peer)
        server.track(upstream)
        return upstream

    monkeypatch.setattr(network, "_open_tunnel", open_fake)
    clients = []
    try:
        with network.browser_proxy(allowed_hosts=("example.test",)) as proxy:
            clients = [connect(proxy) for _ in range(3)]
            clients[0].sendall(b"CON")
            clients[1].sendall(b"CONNECT example.test:443 HTTP/1.1\r\n\r\n")
            assert clients[1].recv(4096).startswith(b"HTTP/1.1 200")
        assert local_proxy[0].socket.fileno() == -1
        assert not local_proxy[0].connections
        for client in clients:
            try:
                assert client.recv(1) == b""
            except ConnectionResetError:
                pass  # Unread partial CONNECT can cause TCP RST on close.
        assert peers[0].recv(1) == b""
    finally:
        for connection in clients + peers:
            connection.close()


def test_shutdown_closes_sockets_while_dns_waits_for_os(local_proxy, monkeypatch):
    # getaddrinfo is synchronous OS work: closing the proxy cannot cancel it.
    # Record that limitation and require no late outbound connection afterwards.
    resolving = threading.Event()
    resolver_return = threading.Event()
    handler_threads = []

    def resolve(host, *args):
        assert host == "example.test"
        handler_threads.append(threading.current_thread())
        resolving.set()
        assert resolver_return.wait(timeout=5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]

    monkeypatch.setattr(network.socket, "getaddrinfo", resolve)
    client = None
    try:
        with network.browser_proxy(allowed_hosts=("example.test",)) as proxy:
            address = urlsplit(proxy["server"])
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect((address.hostname, address.port))
            client.sendall(b"CONNECT example.test:443 HTTP/1.1\r\n\r\n")
            assert resolving.wait(timeout=2)
        assert local_proxy[0].socket.fileno() == -1
        assert not local_proxy[0].connections
        assert client.recv(1) == b""
        assert handler_threads[0].is_alive()  # Honest bound of context shutdown.
        monkeypatch.setattr(network.socket, "socket", lambda *args: pytest.fail("Late outbound after shutdown"))
    finally:
        resolver_return.set()
        for thread in handler_threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        if client:
            client.close()
