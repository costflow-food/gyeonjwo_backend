"""Public-only TCP connections for the supplier crawler (httpcore's backend API).

Resolve at the connection boundary, reject mixed public/private answers, and
connect a socket directly to a validated numeric IP. HTTP Host and TLS SNI
remain the original hostname. No environment proxies or hostname re-resolution
are used to choose the destination after this check.
"""
import ipaddress
import select
import socket
import ssl

import httpcore
import httpx


class UnsafeAddress(ValueError):
    pass


def public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # Do not let transition/mapped forms disguise IPv4 destinations.
        if address.ipv4_mapped or address.sixtofour or address.teredo or address.is_site_local:
            return False
        if address in ipaddress.ip_network('64:ff9b::/96'):
            return False
    return True


def resolve_public(host: str, port: int) -> list[str]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not public_address(host):
            raise UnsafeAddress('Non-public address')
        return [str(literal)]
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError:
        raise httpcore.ConnectError('DNS lookup failed') from None
    addresses = []
    for family, _, _, _, sockaddr in answers:
        if family not in (socket.AF_INET, socket.AF_INET6) or not public_address(sockaddr[0]):
            raise UnsafeAddress('Non-public DNS answer')
        if sockaddr[0] not in addresses:
            addresses.append(sockaddr[0])
    if not addresses:
        raise UnsafeAddress('Empty DNS answer')
    return addresses


class _SocketStream(httpcore.NetworkStream):
    def __init__(self, sock):
        self.sock = sock

    def read(self, max_bytes, timeout=None):
        try:
            self.sock.settimeout(timeout)
            return self.sock.recv(max_bytes)
        except socket.timeout:
            raise httpcore.ReadTimeout('Read timed out') from None
        except OSError:
            raise httpcore.ReadError('Read failed') from None

    def write(self, buffer, timeout=None):
        try:
            self.sock.settimeout(timeout)
            self.sock.sendall(buffer)
        except socket.timeout:
            raise httpcore.WriteTimeout('Write timed out') from None
        except OSError:
            raise httpcore.WriteError('Write failed') from None

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        try:
            self.sock.settimeout(timeout)
            return _SocketStream(ssl_context.wrap_socket(self.sock, server_hostname=server_hostname))
        except socket.timeout:
            self.close()
            raise httpcore.ConnectTimeout('TLS handshake timed out') from None
        except OSError:
            self.close()
            raise httpcore.ConnectError('TLS handshake failed') from None

    def get_extra_info(self, info):
        if info == 'ssl_object' and isinstance(self.sock, ssl.SSLSocket):
            return self.sock
        if info == 'server_addr':
            return self.sock.getpeername()
        if info == 'client_addr':
            return self.sock.getsockname()
        if info == 'socket':
            return self.sock
        if info == 'is_readable':
            return bool(select.select([self.sock], [], [], 0)[0])
        return None

    def close(self):
        self.sock.close()


class _PublicBackend(httpcore.NetworkBackend):
    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = resolve_public(host, port)
        last_error = None
        for address in addresses:
            sock = None
            try:
                family = socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                for option in socket_options or ():
                    sock.setsockopt(*option)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if local_address is not None:
                    sock.bind((local_address, 0))
                # Canonical numeric sockaddr: no create_connection/getaddrinfo
                # on the hostname is permitted between validation and connect.
                sock.connect((address, port))
                peer = sock.getpeername()
                if (not peer or not public_address(peer[0])
                        or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(address)
                        or peer[1] != port):
                    raise UnsafeAddress('Connected peer does not match validated address')
                return _SocketStream(sock)
            except (socket.timeout, OSError) as error:
                last_error = (httpcore.ConnectTimeout('Connection timed out') if isinstance(error, socket.timeout)
                              else httpcore.ConnectError('Connection failed'))
                if sock is not None:
                    sock.close()
            except BaseException:
                if sock is not None:
                    sock.close()
                raise
        raise last_error or httpcore.ConnectError('No public address available')


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        yield from self.stream

    def close(self):
        self.stream.close()


class PublicHTTPTransport(httpx.BaseTransport):
    def __init__(self):
        self.pool = httpcore.ConnectionPool(ssl_context=ssl.create_default_context(),
                                           network_backend=_PublicBackend(), retries=0)

    def handle_request(self, request):
        # Keep origin/Host/SNI intact; pinning belongs to the TCP backend only.
        response = self.pool.handle_request(httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions))
        return httpx.Response(response.status, headers=response.headers,
                              stream=_ResponseStream(response.stream), extensions=response.extensions)

    def close(self):
        self.pool.close()
