"""
:mod:`websockets.legacy.server` defines the WebSocket server APIs.

"""

from __future__ import annotations

import asyncio
import email.utils
import functools
import http
import logging
import socket
import warnings
from types import TracebackType
from typing import (
    Any,
    Awaitable,
    Callable,
    Generator,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
    cast,
)

from ..connection import State
from ..datastructures import Headers, HeadersLike, MultipleValuesError
from ..exceptions import (
    AbortHandshake,
    InvalidHandshake,
    InvalidHeader,
    InvalidMessage,
    InvalidOrigin,
    InvalidUpgrade,
    NegotiationError,
)
from ..extensions import Extension, ServerExtensionFactory
from ..extensions.permessage_deflate import enable_server_permessage_deflate
from ..headers import (
    build_extension,
    parse_extension,
    parse_subprotocol,
    validate_subprotocols,
)
from ..http import USER_AGENT
from ..typing import ExtensionHeader, LoggerLike, Origin, Subprotocol
from .compatibility import loop_if_py_lt_38
from .handshake import build_response, check_request
from .http import read_request
from .protocol import WebSocketCommonProtocol


__all__ = ["serve", "unix_serve", "WebSocketServerProtocol", "WebSocketServer"]


HeadersLikeOrCallable = Union[HeadersLike, Callable[[str, Headers], HeadersLike]]

HTTPResponse = Tuple[http.HTTPStatus, HeadersLike, bytes]


class WebSocketServerProtocol(WebSocketCommonProtocol):
    """
    :class:`~asyncio.Protocol` subclass implementing a WebSocket server.

    :class:`WebSocketServerProtocol`:

    * performs the opening handshake to establish the connection;
    * provides :meth:`recv` and :meth:`send` coroutines for receiving and
      sending messages;
    * deals with control frames automatically;
    * performs the closing handshake to terminate the connection.

    You may customize the opening handshake by subclassing
    :class:`WebSocketServer` and overriding:

    * :meth:`process_request` to intercept the client request before any
      processing and, if appropriate, to abort the WebSocket request and
      return a HTTP response instead;
    * :meth:`select_subprotocol` to select a subprotocol, if the client and
      the server have multiple subprotocols in common and the default logic
      for choosing one isn't suitable (this is rarely needed).

    :class:`WebSocketServerProtocol` supports asynchronous iteration::

        async for message in websocket:
            await process(message)

    The iterator yields incoming messages. It exits normally when the
    connection is closed with the close code 1000 (OK) or 1001 (going away).
    It raises a :exc:`~websockets.exceptions.ConnectionClosedError` exception
    when the connection is closed with any other code.

    Once the connection is open, a `Ping frame`_ is sent every
    ``ping_interval`` seconds. This serves as a keepalive. It helps keeping
    the connection open, especially in the presence of proxies with short
    timeouts on inactive connections. Set ``ping_interval`` to ``None`` to
    disable this behavior.

    .. _Ping frame: https://www.rfc-editor.org/rfc/rfc6455.html#section-5.5.2

    If the corresponding `Pong frame`_ isn't received within ``ping_timeout``
    seconds, the connection is considered unusable and is closed with
    code 1011. This ensures that the remote endpoint remains responsive. Set
    ``ping_timeout`` to ``None`` to disable this behavior.

    .. _Pong frame: https://www.rfc-editor.org/rfc/rfc6455.html#section-5.5.3

    The ``close_timeout`` parameter defines a maximum wait time for completing
    the closing handshake and terminating the TCP connection. For legacy
    reasons, :meth:`close` completes in at most ``4 * close_timeout`` seconds.

    ``close_timeout`` needs to be a parameter of the protocol because
    websockets usually calls :meth:`close` implicitly when the connection
    handler terminates.

    To apply a timeout to any other API, wrap it in :func:`~asyncio.wait_for`.

    The ``max_size`` parameter enforces the maximum size for incoming messages
    in bytes. The default value is 1 MiB. ``None`` disables the limit. If a
    message larger than the maximum size is received, :meth:`recv` will
    raise :exc:`~websockets.exceptions.ConnectionClosedError` and the
    connection will be closed with code 1009.

    The ``max_queue`` parameter sets the maximum length of the queue that
    holds incoming messages. The default value is ``32``. ``None`` disables
    the limit. Messages are added to an in-memory queue when they're received;
    then :meth:`recv` pops from that queue. In order to prevent excessive
    memory consumption when messages are received faster than they can be
    processed, the queue must be bounded. If the queue fills up, the protocol
    stops processing incoming data until :meth:`recv` is called. In this
    situation, various receive buffers (at least in :mod:`asyncio` and in the
    OS) will fill up, then the TCP receive window will shrink, slowing down
    transmission to avoid packet loss.

    Since Python can use up to 4 bytes of memory to represent a single
    character, each connection may use up to ``4 * max_size * max_queue``
    bytes of memory to store incoming messages. By default, this is 128 MiB.
    You may want to lower the limits, depending on your application's
    requirements.

    The ``read_limit`` argument sets the high-water limit of the buffer for
    incoming bytes. The low-water limit is half the high-water limit. The
    default value is 64 KiB, half of asyncio's default (based on the current
    implementation of :class:`~asyncio.StreamReader`).

    The ``write_limit`` argument sets the high-water limit of the buffer for
    outgoing bytes. The low-water limit is a quarter of the high-water limit.
    The default value is 64 KiB, equal to asyncio's default (based on the
    current implementation of ``FlowControlMixin``).

    As soon as the HTTP request and response in the opening handshake are
    processed:

    * the request path is available in the :attr:`path` attribute;
    * the request and response HTTP headers are available in the
      :attr:`request_headers` and :attr:`response_headers` attributes,
      which are :class:`~websockets.http.Headers` instances.

    If a subprotocol was negotiated, it's available in the :attr:`subprotocol`
    attribute.

    Once the connection is closed, the code is available in the
    :attr:`close_code` attribute and the reason in :attr:`close_reason`.

    All attributes must be treated as read-only.

    """

    is_client = False
    side = "server"

    def __init__(
        self,
        ws_handler: Callable[[WebSocketServerProtocol, str], Awaitable[Any]],
        ws_server: WebSocketServer,
        *,
        origins: Optional[Sequence[Optional[Origin]]] = None,
        extensions: Optional[Sequence[ServerExtensionFactory]] = None,
        subprotocols: Optional[Sequence[Subprotocol]] = None,
        extra_headers: Optional[HeadersLikeOrCallable] = None,
        process_request: Optional[
            Callable[[str, Headers], Awaitable[Optional[HTTPResponse]]]
        ] = None,
        select_subprotocol: Optional[
            Callable[[Sequence[Subprotocol], Sequence[Subprotocol]], Subprotocol]
        ] = None,
        logger: Optional[LoggerLike] = None,
        **kwargs: Any,
    ) -> None:
        if logger is None:
            logger = logging.getLogger("websockets.server")
        super().__init__(logger=logger, **kwargs)
        # For backwards compatibility with 6.0 or earlier.
        if origins is not None and "" in origins:
            warnings.warn("use None instead of '' in origins", DeprecationWarning)
            origins = [None if origin == "" else origin for origin in origins]
        self.ws_handler = ws_handler
        self.ws_server = ws_server
        self.origins = origins
        self.available_extensions = extensions
        self.available_subprotocols = subprotocols
        self.extra_headers = extra_headers
        self._process_request = process_request
        self._select_subprotocol = select_subprotocol

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """
        Register connection and initialize a task to handle it.

        """
        super().connection_made(transport)
        # Register the connection with the server before creating the handler
        # task. Registering at the beginning of the handler coroutine would
        # create a race condition between the creation of the task, which
        # schedules its execution, and the moment the handler starts running.
        self.ws_server.register(self)
        self.handler_task = self.loop.create_task(self.handler())

    async def handler(self) -> None:
        """
        Handle the lifecycle of a WebSocket connection.

        Since this method doesn't have a caller able to handle exceptions, it
        attemps to log relevant ones and guarantees that the TCP connection is
        closed before exiting.

        """
        try:

            try:
                path = await self.handshake(
                    origins=self.origins,
                    available_extensions=self.available_extensions,
                    available_subprotocols=self.available_subprotocols,
                    extra_headers=self.extra_headers,
                )
            # Remove this branch when dropping support for Python < 3.8
            # because CancelledError no longer inherits Exception.
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except ConnectionError:
                raise
            except Exception as exc:
                if isinstance(exc, AbortHandshake):
                    status, headers, body = exc.status, exc.headers, exc.body
                elif isinstance(exc, InvalidOrigin):
                    if self.debug:
                        self.logger.debug("! invalid origin", exc_info=True)
                    status, headers, body = (
                        http.HTTPStatus.FORBIDDEN,
                        Headers(),
                        f"Failed to open a WebSocket connection: {exc}.\n".encode(),
                    )
                elif isinstance(exc, InvalidUpgrade):
                    if self.debug:
                        self.logger.debug("! invalid upgrade", exc_info=True)
                    status, headers, body = (
                        http.HTTPStatus.UPGRADE_REQUIRED,
                        Headers([("Upgrade", "websocket")]),
                        (
                            f"Failed to open a WebSocket connection: {exc}.\n"
                            f"\n"
                            f"You cannot access a WebSocket server directly "
                            f"with a browser. You need a WebSocket client.\n"
                        ).encode(),
                    )
                elif isinstance(exc, InvalidHandshake):
                    if self.debug:
                        self.logger.debug("! invalid handshake", exc_info=True)
                    status, headers, body = (
                        http.HTTPStatus.BAD_REQUEST,
                        Headers(),
                        f"Failed to open a WebSocket connection: {exc}.\n".encode(),
                    )
                else:
                    self.logger.error("opening handshake failed", exc_info=True)
                    status, headers, body = (
                        http.HTTPStatus.INTERNAL_SERVER_ERROR,
                        Headers(),
                        (
                            b"Failed to open a WebSocket connection.\n"
                            b"See server log for more information.\n"
                        ),
                    )

                headers.setdefault("Date", email.utils.formatdate(usegmt=True))
                headers.setdefault("Server", USER_AGENT)
                headers.setdefault("Content-Length", str(len(body)))
                headers.setdefault("Content-Type", "text/plain")
                headers.setdefault("Connection", "close")

                self.write_http_response(status, headers, body)
                self.logger.info(
                    "connection failed (%d %s)", status.value, status.phrase
                )
                await self.close_transport()
                return

            try:
                await self.ws_handler(self, path)
            except Exception:
                self.logger.error("connection handler failed", exc_info=True)
                if not self.closed:
                    self.fail_connection(1011)
                raise

            try:
                await self.close()
            except ConnectionError:
                raise
            except Exception:
                self.logger.error("closing handshake failed", exc_info=True)
                raise

        except Exception:
            # Last-ditch attempt to avoid leaking connections on errors.
            try:
                self.transport.close()
            except Exception:  # pragma: no cover
                pass

        finally:
            # Unregister the connection with the server when the handler task
            # terminates. Registration is tied to the lifecycle of the handler
            # task because the server waits for tasks attached to registered
            # connections before terminating.
            self.ws_server.unregister(self)
            self.logger.info("connection closed")

    async def read_http_request(self) -> Tuple[str, Headers]:
        """
        Read request line and headers from the HTTP request.

        If the request contains a body, it may be read from ``self.reader``
        after this coroutine returns.

        :raises ~websockets.exceptions.InvalidMessage: if the HTTP message is
            malformed or isn't an HTTP/1.1 GET request

        """
        try:
            path, headers = await read_request(self.reader)
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as exc:
            raise InvalidMessage("did not receive a valid HTTP request") from exc

        if self.debug:
            self.logger.debug("< GET %s HTTP/1.1", path)
            for key, value in headers.raw_items():
                self.logger.debug("< %s: %s", key, value)

        self.path = path
        self.request_headers = headers

        return path, headers

    def write_http_response(
        self, status: http.HTTPStatus, headers: Headers, body: Optional[bytes] = None
    ) -> None:
        """
        Write status line and headers to the HTTP response.

        This coroutine is also able to write a response body.

        """
        self.response_headers = headers

        if self.debug:
            self.logger.debug("> HTTP/1.1 %d %s", status.value, status.phrase)
            for key, value in headers.raw_items():
                self.logger.debug("> %s: %s", key, value)
            if body is not None:
                self.logger.debug("> [body] (%d bytes)", len(body))

        # Since the status line and headers only contain ASCII characters,
        # we can keep this simple.
        response = f"HTTP/1.1 {status.value} {status.phrase}\r\n"
        response += str(headers)

        self.transport.write(response.encode())

        if body is not None:
            self.transport.write(body)

    async def process_request(
        self, path: str, request_headers: Headers
    ) -> Optional[HTTPResponse]:
        """
        Intercept the HTTP request and return an HTTP response if appropriate.

        If ``process_request`` returns ``None``, the WebSocket handshake
        continues. If it returns 3-uple containing a status code, response
        headers and a response body, that HTTP response is sent and the
        connection is closed. In that case:

        * The HTTP status must be a :class:`~http.HTTPStatus`.
        * HTTP headers must be a :class:`~websockets.http.Headers` instance, a
          :class:`~collections.abc.Mapping`, or an iterable of ``(name,
          value)`` pairs.
        * The HTTP response body must be :class:`bytes`. It may be empty.

        This coroutine may be overridden in a :class:`WebSocketServerProtocol`
        subclass, for example:

        * to return a HTTP 200 OK response on a given path; then a load
          balancer can use this path for a health check;
        * to authenticate the request and return a HTTP 401 Unauthorized or a
          HTTP 403 Forbidden when authentication fails.

        Instead of subclassing, it is possible to override this method by
        passing a ``process_request`` argument to the :func:`serve` function
        or the :class:`WebSocketServerProtocol` constructor. This is
        equivalent, except ``process_request`` won't have access to the
        protocol instance, so it can't store information for later use.

        ``process_request`` is expected to complete quickly. If it may run for
        a long time, then it should await :meth:`wait_closed` and exit if
        :meth:`wait_closed` completes, or else it could prevent the server
        from shutting down.

        :param path: request path, including optional query string
        :param request_headers: request headers

        """
        if self._process_request is not None:
            response = self._process_request(path, request_headers)
            if isinstance(response, Awaitable):
                return await response
            else:
                # For backwards compatibility with 7.0.
                warnings.warn(
                    "declare process_request as a coroutine", DeprecationWarning
                )
                return response  # type: ignore
        return None

    @staticmethod
    def process_origin(
        headers: Headers, origins: Optional[Sequence[Optional[Origin]]] = None
    ) -> Optional[Origin]:
        """
        Handle the Origin HTTP request header.

        :param headers: request headers
        :param origins: optional list of acceptable origins
        :raises ~websockets.exceptions.InvalidOrigin: if the origin isn't
            acceptable

        """
        # "The user agent MUST NOT include more than one Origin header field"
        # per https://www.rfc-editor.org/rfc/rfc6454.html#section-7.3.
        try:
            origin = cast(Optional[Origin], headers.get("Origin"))
        except MultipleValuesError as exc:
            raise InvalidHeader("Origin", "more than one Origin header found") from exc
        if origins is not None:
            if origin not in origins:
                raise InvalidOrigin(origin)
        return origin

    @staticmethod
    def process_extensions(
        headers: Headers,
        available_extensions: Optional[Sequence[ServerExtensionFactory]],
    ) -> Tuple[Optional[str], List[Extension]]:
        """
        Handle the Sec-WebSocket-Extensions HTTP request header.

        Accept or reject each extension proposed in the client request.
        Negotiate parameters for accepted extensions.

        Return the Sec-WebSocket-Extensions HTTP response header and the list
        of accepted extensions.

        :rfc:`6455` leaves the rules up to the specification of each
        :extension.

        To provide this level of flexibility, for each extension proposed by
        the client, we check for a match with each extension available in the
        server configuration. If no match is found, the extension is ignored.

        If several variants of the same extension are proposed by the client,
        it may be accepted several times, which won't make sense in general.
        Extensions must implement their own requirements. For this purpose,
        the list of previously accepted extensions is provided.

        This process doesn't allow the server to reorder extensions. It can
        only select a subset of the extensions proposed by the client.

        Other requirements, for example related to mandatory extensions or the
        order of extensions, may be implemented by overriding this method.

        :param headers: request headers
        :param extensions: optional list of supported extensions
        :raises ~websockets.exceptions.InvalidHandshake: to abort the
            handshake with an HTTP 400 error code

        """
        response_header_value: Optional[str] = None

        extension_headers: List[ExtensionHeader] = []
        accepted_extensions: List[Extension] = []

        header_values = headers.get_all("Sec-WebSocket-Extensions")

        if header_values and available_extensions:

            parsed_header_values: List[ExtensionHeader] = sum(
                [parse_extension(header_value) for header_value in header_values], []
            )

            for name, request_params in parsed_header_values:

                for ext_factory in available_extensions:

                    # Skip non-matching extensions based on their name.
                    if ext_factory.name != name:
                        continue

                    # Skip non-matching extensions based on their params.
                    try:
                        response_params, extension = ext_factory.process_request_params(
                            request_params, accepted_extensions
                        )
                    except NegotiationError:
                        continue

                    # Add matching extension to the final list.
                    extension_headers.append((name, response_params))
                    accepted_extensions.append(extension)

                    # Break out of the loop once we have a match.
                    break

                # If we didn't break from the loop, no extension in our list
                # matched what the client sent. The extension is declined.

        # Serialize extension header.
        if extension_headers:
            response_header_value = build_extension(extension_headers)

        return response_header_value, accepted_extensions

    # Not @staticmethod because it calls self.select_subprotocol()
    def process_subprotocol(
        self, headers: Headers, available_subprotocols: Optional[Sequence[Subprotocol]]
    ) -> Optional[Subprotocol]:
        """
        Handle the Sec-WebSocket-Protocol HTTP request header.

        Return Sec-WebSocket-Protocol HTTP response header, which is the same
        as the selected subprotocol.

        :param headers: request headers
        :param available_subprotocols: optional list of supported subprotocols
        :raises ~websockets.exceptions.InvalidHandshake: to abort the
            handshake with an HTTP 400 error code

        """
        subprotocol: Optional[Subprotocol] = None

        header_values = headers.get_all("Sec-WebSocket-Protocol")

        if header_values and available_subprotocols:

            parsed_header_values: List[Subprotocol] = sum(
                [parse_subprotocol(header_value) for header_value in header_values], []
            )

            subprotocol = self.select_subprotocol(
                parsed_header_values, available_subprotocols
            )

        return subprotocol

    def select_subprotocol(
        self,
        client_subprotocols: Sequence[Subprotocol],
        server_subprotocols: Sequence[Subprotocol],
    ) -> Optional[Subprotocol]:
        """
        Pick a subprotocol among those offered by the client.

        If several subprotocols are supported by the client and the server,
        the default implementation selects the preferred subprotocols by
        giving equal value to the priorities of the client and the server.

        If no subprotocol is supported by the client and the server, it
        proceeds without a subprotocol.

        This is unlikely to be the most useful implementation in practice, as
        many servers providing a subprotocol will require that the client uses
        that subprotocol. Such rules can be implemented in a subclass.

        Instead of subclassing, it is possible to override this method by
        passing a ``select_subprotocol`` argument to the :func:`serve`
        function or the :class:`WebSocketServerProtocol` constructor.

        :param client_subprotocols: list of subprotocols offered by the client
        :param server_subprotocols: list of subprotocols available on the server

        """
        if self._select_subprotocol is not None:
            return self._select_subprotocol(client_subprotocols, server_subprotocols)

        subprotocols = set(client_subprotocols) & set(server_subprotocols)
        if not subprotocols:
            return None
        priority = lambda p: (
            client_subprotocols.index(p) + server_subprotocols.index(p)
        )
        return sorted(subprotocols, key=priority)[0]

    async def handshake(
        self,
        origins: Optional[Sequence[Optional[Origin]]] = None,
        available_extensions: Optional[Sequence[ServerExtensionFactory]] = None,
        available_subprotocols: Optional[Sequence[Subprotocol]] = None,
        extra_headers: Optional[HeadersLikeOrCallable] = None,
    ) -> str:
        """
        Perform the server side of the opening handshake.

        Return the path of the URI of the request.

        :param origins: list of acceptable values of the Origin HTTP header;
            include ``None`` if the lack of an origin is acceptable
        :param available_extensions: list of supported extensions in the order
            in which they should be used
        :param available_subprotocols: list of supported subprotocols in order
            of decreasing preference
        :param extra_headers: sets additional HTTP response headers when the
            handshake succeeds; it can be a :class:`~websockets.http.Headers`
            instance, a :class:`~collections.abc.Mapping`, an iterable of
            ``(name, value)`` pairs, or a callable taking the request path and
            headers in arguments and returning one of the above.
        :raises ~websockets.exceptions.InvalidHandshake: if the handshake
            fails

        """
        path, request_headers = await self.read_http_request()

        # Hook for customizing request handling, for example checking
        # authentication or treating some paths as plain HTTP endpoints.
        early_response_awaitable = self.process_request(path, request_headers)
        if isinstance(early_response_awaitable, Awaitable):
            early_response = await early_response_awaitable
        else:
            # For backwards compatibility with 7.0.
            warnings.warn("declare process_request as a coroutine", DeprecationWarning)
            early_response = early_response_awaitable  # type: ignore

        # The connection may drop while process_request is running.
        if self.state is State.CLOSED:
            raise self.connection_closed_exc()  # pragma: no cover

        # Change the response to a 503 error if the server is shutting down.
        if not self.ws_server.is_serving():
            early_response = (
                http.HTTPStatus.SERVICE_UNAVAILABLE,
                [],
                b"Server is shutting down.\n",
            )

        if early_response is not None:
            raise AbortHandshake(*early_response)

        key = check_request(request_headers)

        self.origin = self.process_origin(request_headers, origins)

        extensions_header, self.extensions = self.process_extensions(
            request_headers, available_extensions
        )

        protocol_header = self.subprotocol = self.process_subprotocol(
            request_headers, available_subprotocols
        )

        response_headers = Headers()

        build_response(response_headers, key)

        if extensions_header is not None:
            response_headers["Sec-WebSocket-Extensions"] = extensions_header

        if protocol_header is not None:
            response_headers["Sec-WebSocket-Protocol"] = protocol_header

        if callable(extra_headers):
            extra_headers = extra_headers(path, self.request_headers)
        if extra_headers is not None:
            response_headers.update(extra_headers)

        response_headers.setdefault("Date", email.utils.formatdate(usegmt=True))
        response_headers.setdefault("Server", USER_AGENT)

        self.write_http_response(http.HTTPStatus.SWITCHING_PROTOCOLS, response_headers)

        self.logger.info("connection open")

        self.connection_open()

        return path


class WebSocketServer:
    """
    WebSocket server returned by :func:`serve`.

    This class provides the same interface as
    :class:`~asyncio.AbstractServer`, namely the
    :meth:`~asyncio.AbstractServer.close` and
    :meth:`~asyncio.AbstractServer.wait_closed` methods.

    It keeps track of WebSocket connections in order to close them properly
    when shutting down.

    Instances of this class store a reference to the :class:`~asyncio.Server`
    object returned by :meth:`~asyncio.loop.create_server` rather than inherit
    from :class:`~asyncio.Server` in part because
    :meth:`~asyncio.loop.create_server` doesn't support passing a custom
    :class:`~asyncio.Server` class.

    """

    def __init__(
        self, loop: asyncio.AbstractEventLoop, logger: Optional[LoggerLike] = None
    ) -> None:
        # Store a reference to loop to avoid relying on self.server._loop.
        self.loop = loop

        if logger is None:
            logger = logging.getLogger("websockets.server")
        self.logger = logger

        # Keep track of active connections.
        self.websockets: Set[WebSocketServerProtocol] = set()

        # Task responsible for closing the server and terminating connections.
        self.close_task: Optional[asyncio.Task[None]] = None

        # Completed when the server is closed and connections are terminated.
        self.closed_waiter: asyncio.Future[None] = loop.create_future()

    def wrap(self, server: asyncio.AbstractServer) -> None:
        """
        Attach to a given :class:`~asyncio.Server`.

        Since :meth:`~asyncio.loop.create_server` doesn't support injecting a
        custom ``Server`` class, the easiest solution that doesn't rely on
        private :mod:`asyncio` APIs is to:

        - instantiate a :class:`WebSocketServer`
        - give the protocol factory a reference to that instance
        - call :meth:`~asyncio.loop.create_server` with the factory
        - attach the resulting :class:`~asyncio.Server` with this method

        """
        self.server = server
        assert server.sockets is not None
        for sock in server.sockets:
            if sock.family == socket.AF_INET:
                name = "%s:%d" % sock.getsockname()
            elif sock.family == socket.AF_INET6:
                name = "[%s]:%d" % sock.getsockname()[:2]
            elif sock.family == socket.AF_UNIX:
                name = sock.getsockname()
            # In the unlikely event that someone runs websockets over a
            # protocol other than IP or Unix sockets, avoid crashing.
            else:  # pragma: no cover
                name = str(sock.getsockname())
            self.logger.info("server listening on %s", name)

    def register(self, protocol: WebSocketServerProtocol) -> None:
        """
        Register a connection with this server.

        """
        self.websockets.add(protocol)

    def unregister(self, protocol: WebSocketServerProtocol) -> None:
        """
        Unregister a connection with this server.

        """
        self.websockets.remove(protocol)

    def is_serving(self) -> bool:
        """
        Tell whether the server is accepting new connections or shutting down.

        """
        return self.server.is_serving()

    def close(self) -> None:
        """
        Close the server.

        This method:

        * closes the underlying :class:`~asyncio.Server`;
        * rejects new WebSocket connections with an HTTP 503 (service
          unavailable) error; this happens when the server accepted the TCP
          connection but didn't complete the WebSocket opening handshake prior
          to closing;
        * closes open WebSocket connections with close code 1001 (going away).

        :meth:`close` is idempotent.

        """
        if self.close_task is None:
            self.close_task = self.loop.create_task(self._close())

    async def _close(self) -> None:
        """
        Implementation of :meth:`close`.

        This calls :meth:`~asyncio.Server.close` on the underlying
        :class:`~asyncio.Server` object to stop accepting new connections and
        then closes open connections with close code 1001.

        """
        self.logger.info("server closing")

        # Stop accepting new connections.
        self.server.close()

        # Wait until self.server.close() completes.
        await self.server.wait_closed()

        # Wait until all accepted connections reach connection_made() and call
        # register(). See https://bugs.python.org/issue34852 for details.
        await asyncio.sleep(0, **loop_if_py_lt_38(self.loop))

        # Close OPEN connections with status code 1001. Since the server was
        # closed, handshake() closes OPENING connections with a HTTP 503
        # error. Wait until all connections are closed.

        # asyncio.wait doesn't accept an empty first argument
        if self.websockets:
            await asyncio.wait(
                [
                    asyncio.create_task(websocket.close(1001))
                    for websocket in self.websockets
                ],
                **loop_if_py_lt_38(self.loop),
            )

        # Wait until all connection handlers are complete.

        # asyncio.wait doesn't accept an empty first argument.
        if self.websockets:
            await asyncio.wait(
                [websocket.handler_task for websocket in self.websockets],
                **loop_if_py_lt_38(self.loop),
            )

        # Tell wait_closed() to return.
        self.closed_waiter.set_result(None)

        self.logger.info("server closed")

    async def wait_closed(self) -> None:
        """
        Wait until the server is closed.

        When :meth:`wait_closed` returns, all TCP connections are closed and
        all connection handlers have returned.

        """
        await asyncio.shield(self.closed_waiter)

    @property
    def sockets(self) -> Optional[List[socket.socket]]:
        """
        List of :class:`~socket.socket` objects the server is listening to.

        ``None`` if the server is closed.

        """
        return self.server.sockets


class Serve:
    """

    Create, start, and return a WebSocket server on ``host`` and ``port``.

    Whenever a client connects, the server accepts the connection, creates a
    :class:`WebSocketServerProtocol`, performs the opening handshake, and
    delegates to the connection handler defined by ``ws_handler``. Once the
    handler completes, either normally or with an exception, the server
    performs the closing handshake and closes the connection.

    Awaiting :func:`serve` yields a :class:`WebSocketServer`. This instance
    provides :meth:`~WebSocketServer.close` and
    :meth:`~WebSocketServer.wait_closed` methods for terminating the server
    and cleaning up its resources.

    When a server is closed with :meth:`~WebSocketServer.close`, it closes all
    connections with close code 1001 (going away). Connections handlers, which
    are running the ``ws_handler`` coroutine, will receive a
    :exc:`~websockets.exceptions.ConnectionClosedOK` exception on their
    current or next interaction with the WebSocket connection.

    :func:`serve` can be used as an asynchronous context manager::

        stop = asyncio.Future()  # set this future to exit the server

        async with serve(...):
            await stop

    In this case, the server is shut down when exiting the context.

    :func:`serve` is a wrapper around the event loop's
    :meth:`~asyncio.loop.create_server` method. It creates and starts a
    :class:`asyncio.Server` with :meth:`~asyncio.loop.create_server`. Then it
    wraps the :class:`asyncio.Server` in a :class:`WebSocketServer`  and
    returns the :class:`WebSocketServer`.

    ``ws_handler`` is the WebSocket handler. It must be a coroutine accepting
    two arguments: the WebSocket connection, which is an instance of
    :class:`WebSocketServerProtocol`, and the path of the request.

    The ``host`` and ``port`` arguments, as well as unrecognized keyword
    arguments, are passed to :meth:`~asyncio.loop.create_server`.

    For example, you can set the ``ssl`` keyword argument to a
    :class:`~ssl.SSLContext` to enable TLS.

    ``create_protocol`` defaults to :class:`WebSocketServerProtocol`. It may
    be replaced by a wrapper or a subclass to customize the protocol that
    manages the connection.

    The behavior of ``ping_interval``, ``ping_timeout``, ``close_timeout``,
    ``max_size``, ``max_queue``, ``read_limit``, and ``write_limit`` is
    described in :class:`WebSocketServerProtocol`.

    :func:`serve` also accepts the following optional arguments:

    * ``compression`` is a shortcut to configure compression extensions;
      by default it enables the "permessage-deflate" extension; set it to
      ``None`` to disable compression.
    * ``origins`` defines acceptable Origin HTTP headers; include ``None`` in
      the list if the lack of an origin is acceptable.
    * ``extensions`` is a list of supported extensions in order of
      decreasing preference.
    * ``subprotocols`` is a list of supported subprotocols in order of
      decreasing preference.
    * ``extra_headers`` sets additional HTTP response headers  when the
      handshake succeeds; it can be a :class:`~websockets.http.Headers`
      instance, a :class:`~collections.abc.Mapping`, an iterable of ``(name,
      value)`` pairs, or a callable taking the request path and headers in
      arguments and returning one of the above.
    * ``process_request`` allows intercepting the HTTP request; it must be a
      coroutine taking the request path and headers in argument; see
      :meth:`~WebSocketServerProtocol.process_request` for details.
    * ``select_subprotocol`` allows customizing the logic for selecting a
      subprotocol; it must be a callable taking the subprotocols offered by
      the client and available on the server in argument; see
      :meth:`~WebSocketServerProtocol.select_subprotocol` for details.

    Since there's no useful way to propagate exceptions triggered in handlers,
    they're sent to the ``"websockets.server"`` logger instead.
    Debugging is much easier if you configure logging to print them::

        import logging
        logger = logging.getLogger("websockets.server")
        logger.setLevel(logging.ERROR)
        logger.addHandler(logging.StreamHandler())

    """

    def __init__(
        self,
        ws_handler: Callable[[WebSocketServerProtocol, str], Awaitable[Any]],
        host: Optional[Union[str, Sequence[str]]] = None,
        port: Optional[int] = None,
        *,
        create_protocol: Optional[Callable[[Any], WebSocketServerProtocol]] = None,
        ping_interval: Optional[float] = 20,
        ping_timeout: Optional[float] = 20,
        close_timeout: Optional[float] = None,
        max_size: Optional[int] = 2 ** 20,
        max_queue: Optional[int] = 2 ** 5,
        read_limit: int = 2 ** 16,
        write_limit: int = 2 ** 16,
        compression: Optional[str] = "deflate",
        origins: Optional[Sequence[Optional[Origin]]] = None,
        extensions: Optional[Sequence[ServerExtensionFactory]] = None,
        subprotocols: Optional[Sequence[Subprotocol]] = None,
        extra_headers: Optional[HeadersLikeOrCallable] = None,
        process_request: Optional[
            Callable[[str, Headers], Awaitable[Optional[HTTPResponse]]]
        ] = None,
        select_subprotocol: Optional[
            Callable[[Sequence[Subprotocol], Sequence[Subprotocol]], Subprotocol]
        ] = None,
        logger: Optional[LoggerLike] = None,
        **kwargs: Any,
    ) -> None:
        # Backwards compatibility: close_timeout used to be called timeout.
        timeout: Optional[float] = kwargs.pop("timeout", None)
        if timeout is None:
            timeout = 10
        else:
            warnings.warn("rename timeout to close_timeout", DeprecationWarning)
        # If both are specified, timeout is ignored.
        if close_timeout is None:
            close_timeout = timeout

        # Backwards compatibility: create_protocol used to be called klass.
        klass: Optional[Type[WebSocketServerProtocol]] = kwargs.pop("klass", None)
        if klass is None:
            klass = WebSocketServerProtocol
        else:
            warnings.warn("rename klass to create_protocol", DeprecationWarning)
        # If both are specified, klass is ignored.
        if create_protocol is None:
            create_protocol = klass

        # Backwards compatibility: recv() used to return None on closed connections
        legacy_recv: bool = kwargs.pop("legacy_recv", False)

        # Backwards compatibility: the loop parameter used to be supported.
        _loop: Optional[asyncio.AbstractEventLoop] = kwargs.pop("loop", None)
        if _loop is None:
            loop = asyncio.get_event_loop()
        else:
            loop = _loop
            warnings.warn("remove loop argument", DeprecationWarning)

        ws_server = WebSocketServer(logger=logger, loop=loop)

        secure = kwargs.get("ssl") is not None

        if compression == "deflate":
            extensions = enable_server_permessage_deflate(extensions)
        elif compression is not None:
            raise ValueError(f"unsupported compression: {compression}")

        if subprotocols is not None:
            validate_subprotocols(subprotocols)

        factory = functools.partial(
            create_protocol,
            ws_handler,
            ws_server,
            host=host,
            port=port,
            secure=secure,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            close_timeout=close_timeout,
            max_size=max_size,
            max_queue=max_queue,
            read_limit=read_limit,
            write_limit=write_limit,
            loop=_loop,
            legacy_recv=legacy_recv,
            origins=origins,
            extensions=extensions,
            subprotocols=subprotocols,
            extra_headers=extra_headers,
            process_request=process_request,
            select_subprotocol=select_subprotocol,
            logger=logger,
        )

        if kwargs.pop("unix", False):
            path: Optional[str] = kwargs.pop("path", None)
            # unix_serve(path) must not specify host and port parameters.
            assert host is None and port is None
            create_server = functools.partial(
                loop.create_unix_server, factory, path, **kwargs
            )
        else:
            create_server = functools.partial(
                loop.create_server, factory, host, port, **kwargs
            )

        # This is a coroutine function.
        self._create_server = create_server
        self.ws_server = ws_server

    # async with serve(...)

    async def __aenter__(self) -> WebSocketServer:
        return await self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.ws_server.close()
        await self.ws_server.wait_closed()

    # await serve(...)

    def __await__(self) -> Generator[Any, None, WebSocketServer]:
        # Create a suitable iterator by calling __await__ on a coroutine.
        return self.__await_impl__().__await__()

    async def __await_impl__(self) -> WebSocketServer:
        server = await self._create_server()
        self.ws_server.wrap(server)
        return self.ws_server

    # yield from serve(...)

    __iter__ = __await__


serve = Serve


def unix_serve(
    ws_handler: Callable[[WebSocketServerProtocol, str], Awaitable[Any]],
    path: Optional[str] = None,
    **kwargs: Any,
) -> Serve:
    """
    Similar to :func:`serve`, but for listening on Unix sockets.

    This function calls the event loop's
    :meth:`~asyncio.loop.create_unix_server` method.

    It is only available on Unix.

    It's useful for deploying a server behind a reverse proxy such as nginx.

    :param path: file system path to the Unix socket

    """
    return serve(ws_handler, path=path, unix=True, **kwargs)
