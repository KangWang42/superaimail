"""Outgoing connections for mail and AI requests: direct, or through an authenticated SOCKS5 proxy.

A proxy is a dict with host, port and optional username/password. The target host name is handed to the
proxy (address type 3), so names that do not resolve on this machine still work.
"""
import imaplib
import smtplib
import socket
import ssl
import struct
import urllib.parse


class NetError(OSError):
    pass


def _recv(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise NetError("代理提前断开了连接")
        data += chunk
    return data


def open_socket(host, port, proxy=None, timeout=25):
    """A connected TCP socket to host:port."""
    if not proxy:
        return socket.create_connection((host, int(port)), timeout=timeout)
    sock = socket.create_connection((proxy["host"], int(proxy["port"])), timeout=timeout)
    try:
        user = (proxy.get("username") or "").encode()
        secret = (proxy.get("password") or "").encode()
        sock.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
        version, method = _recv(sock, 2)
        if version != 5 or method not in (0, 2):
            raise NetError("代理不接受这种连接方式")
        if method == 2:
            if not user:
                raise NetError("代理要求用户名和密码")
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret)
            if _recv(sock, 2)[1] != 0:
                raise NetError("代理用户名或密码不对")
        name = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", int(port)))
        head = _recv(sock, 4)
        if head[1] != 0:
            raise NetError("代理无法连接 %s（代码 %d）" % (host, head[1]))
        kind = head[3]
        _recv(sock, 4 if kind == 1 else 16 if kind == 4 else _recv(sock, 1)[0])
        _recv(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


def tls_context():
    return ssl.create_default_context()


class IMAP(imaplib.IMAP4_SSL):
    """IMAP over TLS (port 993 style), optionally through a SOCKS5 proxy."""

    def __init__(self, host, port=993, proxy=None, timeout=25):
        self._proxy = proxy
        super().__init__(host, int(port), ssl_context=tls_context(), timeout=timeout)

    def _create_socket(self, timeout):
        sock = open_socket(self.host, self.port, self._proxy, timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


class SMTPS(smtplib.SMTP_SSL):
    """SMTP with TLS from the first byte (465, 994), optionally through a proxy."""

    def __init__(self, host, port, proxy=None, timeout=25):
        self._proxy = proxy
        super().__init__(host, int(port), context=tls_context(), timeout=timeout)

    def _get_socket(self, host, port, timeout):
        sock = open_socket(host, port, self._proxy, timeout)
        return self.context.wrap_socket(sock, server_hostname=self._host)


class SMTPStartTLS(smtplib.SMTP):
    """SMTP that upgrades with STARTTLS (587), optionally through a proxy."""

    def __init__(self, host, port, proxy=None, timeout=25):
        self._proxy = proxy
        super().__init__(host, int(port), timeout=timeout)
        self.starttls(context=tls_context())

    def _get_socket(self, host, port, timeout):
        return open_socket(host, port, self._proxy, timeout)


def valid_ai_url(url):
    parts = urllib.parse.urlsplit(url)
    return bool(parts.hostname and not parts.username and not parts.password and not parts.fragment and
                (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"))))


def https_request(url, body, headers, proxy=None, timeout=60):
    """POST JSON over HTTPS, through the proxy when one is given. Returns (status, bytes)."""
    import http.client
    import urllib.parse
    parts = urllib.parse.urlsplit(url)
    if not valid_ai_url(url):
        raise NetError("接口需使用 HTTPS，本机模型可使用回环 HTTP 地址")
    port = parts.port or (443 if parts.scheme == "https" else 80)

    class Connection(http.client.HTTPSConnection):
        def connect(self):
            raw = open_socket(parts.hostname, port, proxy, timeout)
            self.sock = tls_context().wrap_socket(raw, server_hostname=parts.hostname)

    connection = Connection(parts.hostname, port, timeout=timeout) if parts.scheme == "https" else http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
    try:
        path = parts.path + ("?" + parts.query if parts.query else "")
        connection.request("POST", path, body=body, headers=headers)
        reply = connection.getresponse()
        return reply.status, reply.read(4 * 1024 * 1024)
    finally:
        connection.close()
