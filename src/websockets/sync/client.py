from __future__ import annotations

import socket
import ssl
import threading
from types import TracebackType
from typing import Any, Optional, Sequence, Type

from ..client import ClientConnection
from ..connection import Event, State
from ..datastructures import HeadersLike
from ..extensions.base import ClientExtensionFactory
from ..extensions.permessage_deflate import enable_client_permessage_deflate
from ..headers import validate_subprotocols
from ..http11 import Response
from ..typing import LoggerLike, Origin, Subprotocol
from .protocol import Protocol
from .utils import Deadline


__all__ = ["ClientProtocol", "connect", "unix_connect"]


class ClientProtocol(Protocol):
    def __init__(
        self,
        sock: socket.socket,
        connection: ClientConnection,
        ping_interval: Optional[float] = None,
        ping_timeout: Optional[float] = None,
        close_timeout: Optional[float] = None,
    ) -> None:
        super().__init__(
            sock,
            connection,
            ping_interval,
            ping_timeout,
            close_timeout,
        )
        self.response_rcvd = threading.Event()

    def handshake(self, timeout: Optional[float] = None) -> None:
        """
        Perform the opening handshake.

        """
        assert isinstance(self.connection, ClientConnection)

        with self.conn_mutex:
            self.request = self.connection.connect()
            self.connection.send_request(self.request)
            self.send_data()

        if not self.response_rcvd.wait(timeout):
            raise TimeoutError("timed out waiting for handshake response")
        assert self.response is not None

        if self.connection.state is not State.OPEN:
            self.tcp_close()

        if self.response.exception is not None:
            raise self.response.exception

    def process_event(self, event: Event) -> None:
        """
        Process one incoming event.

        """
        # First event - handshake response.
        if self.response is None:
            assert isinstance(event, Response)
            self.response = event
            self.response_rcvd.set()
        # Later events - frames.
        else:
            super().process_event(event)


class Connect:
    def __init__(
        self,
        uri: str,
        *,
        sock: Optional[socket.socket] = None,
        unix: bool = False,
        path: Optional[str] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        server_hostname: Optional[str] = None,
        create_protocol: Optional[Type[ClientProtocol]] = None,
        open_timeout: Optional[float] = None,
        ping_interval: Optional[float] = None,
        ping_timeout: Optional[float] = None,
        close_timeout: Optional[float] = None,
        origin: Optional[Origin] = None,
        extensions: Optional[Sequence[ClientExtensionFactory]] = None,
        subprotocols: Optional[Sequence[Subprotocol]] = None,
        extra_headers: Optional[HeadersLike] = None,
        max_size: Optional[int] = 2 ** 20,
        compression: Optional[str] = "deflate",
        logger: Optional[LoggerLike] = None,
    ) -> None:
        if create_protocol is None:
            create_protocol = ClientProtocol

        if compression == "deflate":
            extensions = enable_client_permessage_deflate(extensions)
        elif compression is not None:
            raise ValueError(f"unsupported compression: {compression}")

        if subprotocols is not None:
            validate_subprotocols(subprotocols)

        # Initialize WebSocket connection

        connection = ClientConnection(
            uri,
            origin,
            extensions,
            subprotocols,
            extra_headers,
            State.CONNECTING,
            max_size,
            logger,
        )
        wsuri = connection.wsuri

        deadline = Deadline(open_timeout)

        # Connect socket

        if sock is None:
            if unix:
                if path is None:
                    raise TypeError("missing path argument")
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(deadline.timeout())
                sock.connect(path)
            else:
                sock = socket.create_connection(
                    (wsuri.host, wsuri.port),
                    deadline.timeout(),
                )
            sock.settimeout(None)
        else:
            if path is not None:
                raise TypeError("path and sock arguments are incompatible")

        # Wrap socket with TLS - there's no way to apply a timeout here

        if wsuri.secure:
            if ssl_context is None:
                ssl_context = ssl.create_default_context()
            if server_hostname is None:
                server_hostname = wsuri.host
            sock = ssl_context.wrap_socket(sock, server_hostname=server_hostname)
        elif ssl_context is not None:
            raise TypeError("ssl_context argument is incompatible with a ws:// URI")

        # Initialize WebSocket protocol

        self.protocol = create_protocol(
            sock,
            connection,
            ping_interval,
            ping_timeout,
            close_timeout,
        )
        self.protocol.handshake(deadline.timeout())

    # with connect(...)

    def __enter__(self) -> ClientProtocol:
        return self.protocol

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.protocol.close()


connect = Connect


def unix_connect(
    path: Optional[str] = None,
    uri: str = "ws://localhost/",
    **kwargs: Any,
) -> Connect:
    """
    Similar to :func:`connect`, but for connecting to a Unix socket.

    This function is only available on Unix.

    It's mainly useful for debugging servers listening on Unix sockets.

    :param path: file system path to the Unix socket
    :param uri: WebSocket URI

    """
    return connect(uri=uri, path=path, unix=True, **kwargs)
