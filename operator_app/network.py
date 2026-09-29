"""A scoped CONNECT tunnel for portals affected by a macOS VPN route.

TLS remains end-to-end between Playwright and the portal. This proxy never
examines TLS payloads, replays requests, or changes system network settings.
"""
from contextlib import contextmanager
import ipaddress
import os
import re
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time


class NetworkError(ValueError):
    pass


def _command(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3,
                                check=False)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _ipv4_interface(name):
    value = _command("/usr/sbin/ipconfig", "getifaddr", name).strip()
    try:
        address = ipaddress.IPv4Address(value)
        return not (address.is_unspecified or address.is_loopback or address.is_link_local)
    except ValueError:
        return False


def _network_interface():
    configured = os.environ.get("OPERATOR_NETWORK_INTERFACE", "auto").strip()
    if configured.lower() in {"off", "none", "disabled"}:
        return None
    automatic = configured.lower() in {"", "auto"}
    if sys.platform != "darwin":
        if not automatic:
            raise NetworkError("Выбор сетевого интерфейса поддерживается только на macOS.")
        return None
    if not automatic:
        if not re.fullmatch(r"en[0-9]{1,3}", configured) or not _ipv4_interface(configured):
            raise NetworkError("Выбранный физический сетевой интерфейс недоступен.")
        return configured
    state = _command("/usr/sbin/scutil", "--nwi")
    active = re.findall(r"^\s*(\w+)\s+: flags\s*:", state, re.MULTILINE)
    if not any(name.startswith("utun") for name in active):
        return None
    for name in active:
        if re.fullmatch(r"en[0-9]{1,3}", name) and _ipv4_interface(name):
            return name
    return None


def _bind_interface(sock, interface):
    # Darwin IP_BOUND_IF is essential: binding a source address alone still
    # allows the VPN's route to intercept the outgoing connection.
    try:
        sock.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_BOUND_IF", 25),
                        socket.if_nametoindex(interface))
    except OSError:
        raise NetworkError("Не удалось привязать соединение к физической сети.") from None


def _allowed(host, patterns):
    return any(host == pattern.lstrip(".") or
               (pattern.startswith(".") and host.endswith(pattern))
               for pattern in patterns)


def _valid_hostname(host):
    # Validate the canonical DNS name before suffix matching or resolution.
    # Empty/overlong labels and repeated trailing dots must not be repaired
    # into a different allowed host. A single FQDN dot is removed by callers.
    labels = host.split(".")
    return (len(host) <= 253 and len(labels) >= 2 and
            all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels))


def _open_tunnel(host, interface, server):
    deadline = time.monotonic() + 15
    try:
        addresses = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        raise NetworkError("Не удалось определить адрес сервиса.") from None
    for family, kind, protocol, _, address in addresses[:4]:
        if server.stopping.is_set():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection = socket.socket(family, kind, protocol)
        server.track(connection)
        try:
            _bind_interface(connection, interface)
            connection.settimeout(remaining)
            connection.connect(address)
            connection.settimeout(15)
            return connection
        except NetworkError:
            server.release(connection)
            raise
        except OSError:
            server.release(connection)
    raise NetworkError("Сервис не ответил через физическую сеть.")


class _TunnelHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server, client = self.server, self.request
        upstream = None
        server.track(client)
        try:
            header_deadline = time.monotonic() + 5
            header = b""
            while b"\r\n\r\n" not in header:
                remaining = header_deadline - time.monotonic()
                if remaining <= 0:
                    return
                client.settimeout(remaining)
                part = client.recv(4096)
                if not part:
                    return
                header += part
                end = header.find(b"\r\n\r\n")
                header_size = end + 4 if end >= 0 else len(header)
                if header_size > 16384:
                    client.sendall(b"HTTP/1.1 431 Request Header Fields Too Large\r\nConnection: close\r\n\r\n")
                    return
            head, pending = header.split(b"\r\n\r\n", 1)
            first = head.split(b"\r\n", 1)[0]
            match = re.fullmatch(rb"CONNECT ([a-zA-Z0-9.-]+):443 HTTP/1\.[01]", first)
            host = match[1].decode("ascii").lower().removesuffix(".") if match else ""
            if not match or not _valid_hostname(host) or not _allowed(host, server.allowed_hosts):
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                return
            try:
                upstream = _open_tunnel(host, server.interface, server)
            except (OSError, NetworkError):
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            client.settimeout(15)
            if pending:
                upstream.sendall(pending)
            peers = {client: upstream, upstream: client}
            last_activity = time.monotonic()
            while not server.stopping.is_set():
                readable, _, _ = select.select(list(peers), [], [], 0.5)
                if not readable:
                    if time.monotonic() - last_activity > 180:
                        return
                    continue
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    peers[source].sendall(data)
                    last_activity = time.monotonic()
        except (OSError, ValueError):
            # Expected disconnects must not print URLs, session state, or TLS data.
            pass
        finally:
            if upstream is not None:
                server.release(upstream)
            server.release(client)


class _TunnelServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, interface, allowed_hosts):
        self.interface = interface
        self.allowed_hosts = allowed_hosts
        self.stopping = threading.Event()
        self.connections = set()
        self.connection_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _TunnelHandler)

    def track(self, connection):
        with self.connection_lock:
            if self.stopping.is_set():
                connection.close()
                raise OSError("Proxy stopped")
            self.connections.add(connection)

    def release(self, connection):
        with self.connection_lock:
            self.connections.discard(connection)
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()

    def close_connections(self):
        self.stopping.set()
        with self.connection_lock:
            connections = tuple(self.connections)
        for connection in connections:
            self.release(connection)

    def handle_error(self, request, client_address):
        # socketserver's default prints exception details to stderr.
        pass


@contextmanager
def browser_proxy(*, allowed_hosts):
    """Yield a Playwright proxy dict, or {} when ordinary routing is appropriate.

    Allowlist entries are exact hosts; a leading dot also permits subdomains.
    OPERATOR_NETWORK_INTERFACE accepts auto (default), off, or an active enN.
    Explicit interface selection fails closed; TLS verification is unchanged.
    """
    interface = _network_interface()
    if interface is None:
        yield {}
        return
    patterns = tuple(str(host).lower().removesuffix(".") for host in allowed_hosts)
    if not patterns or any(not _valid_hostname(host.removeprefix(".")) for host in patterns):
        raise NetworkError("Укажите разрешённые домены для сетевого подключения.")
    # Check binding before launching a browser, so a requested interface can
    # never silently fall back to the ordinary VPN route.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        _bind_interface(probe, interface)
    server = _TunnelServer(interface, patterns)
    thread = None
    started = False
    try:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                                  name="operator-network", daemon=True)
        thread.start()
        started = True
        yield {"server": f"http://127.0.0.1:{server.server_address[1]}"}
    finally:
        server.close_connections()
        # shutdown() waits for serve_forever and would deadlock if start failed.
        if started:
            server.shutdown()
        server.server_close()
        if started:
            thread.join(timeout=2)
