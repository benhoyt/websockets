"""
:mod:`websockets.legacy.framing` reads and writes WebSocket frames.

It deals with a single frame at a time. Anything that depends on the sequence
of frames is implemented in :mod:`websockets.legacy.protocol`.

See `section 5 of RFC 6455`_.

.. _section 5 of RFC 6455: https://www.rfc-editor.org/rfc/rfc6455.html#section-5

"""

from __future__ import annotations

import dataclasses
import struct
from typing import Any, Awaitable, Callable, NamedTuple, Optional, Sequence, Tuple

from .. import extensions, frames
from ..exceptions import PayloadTooBig, ProtocolError


try:
    from ..speedups import apply_mask
except ImportError:  # pragma: no cover
    from ..utils import apply_mask


class Frame(NamedTuple):

    fin: bool
    opcode: frames.Opcode
    data: bytes
    rsv1: bool = False
    rsv2: bool = False
    rsv3: bool = False

    @property
    def new_frame(self) -> frames.Frame:
        return frames.Frame(
            self.opcode,
            self.data,
            self.fin,
            self.rsv1,
            self.rsv2,
            self.rsv3,
        )

    def __str__(self) -> str:
        return str(self.new_frame)

    def check(self) -> None:
        return self.new_frame.check()

    @classmethod
    async def read(
        cls,
        reader: Callable[[int], Awaitable[bytes]],
        *,
        mask: bool,
        max_size: Optional[int] = None,
        extensions: Optional[Sequence[extensions.Extension]] = None,
    ) -> "Frame":
        """
        Read a WebSocket frame.

        :param reader: coroutine that reads exactly the requested number of
            bytes, unless the end of file is reached
        :param mask: whether the frame should be masked i.e. whether the read
            happens on the server side
        :param max_size: maximum payload size in bytes
        :param extensions: list of classes with a ``decode()`` method that
            transforms the frame and return a new frame; extensions are applied
            in reverse order
        :raises ~websockets.exceptions.PayloadTooBig: if the frame exceeds
            ``max_size``
        :raises ~websockets.exceptions.ProtocolError: if the frame
            contains incorrect values

        """

        # Read the header.
        data = await reader(2)
        head1, head2 = struct.unpack("!BB", data)

        # While not Pythonic, this is marginally faster than calling bool().
        fin = True if head1 & 0b10000000 else False
        rsv1 = True if head1 & 0b01000000 else False
        rsv2 = True if head1 & 0b00100000 else False
        rsv3 = True if head1 & 0b00010000 else False

        try:
            opcode = frames.Opcode(head1 & 0b00001111)
        except ValueError as exc:
            raise ProtocolError("invalid opcode") from exc

        if (True if head2 & 0b10000000 else False) != mask:
            raise ProtocolError("incorrect masking")

        length = head2 & 0b01111111
        if length == 126:
            data = await reader(2)
            (length,) = struct.unpack("!H", data)
        elif length == 127:
            data = await reader(8)
            (length,) = struct.unpack("!Q", data)
        if max_size is not None and length > max_size:
            raise PayloadTooBig(f"over size limit ({length} > {max_size} bytes)")
        if mask:
            mask_bits = await reader(4)

        # Read the data.
        data = await reader(length)
        if mask:
            data = apply_mask(data, mask_bits)

        new_frame = frames.Frame(opcode, data, fin, rsv1, rsv2, rsv3)

        if extensions is None:
            extensions = []
        for extension in reversed(extensions):
            new_frame = extension.decode(new_frame, max_size=max_size)

        new_frame.check()

        return cls(
            new_frame.fin,
            new_frame.opcode,
            new_frame.data,
            new_frame.rsv1,
            new_frame.rsv2,
            new_frame.rsv3,
        )

    def write(
        self,
        write: Callable[[bytes], Any],
        *,
        mask: bool,
        extensions: Optional[Sequence[extensions.Extension]] = None,
    ) -> None:
        """
        Write a WebSocket frame.

        :param frame: frame to write
        :param write: function that writes bytes
        :param mask: whether the frame should be masked i.e. whether the write
            happens on the client side
        :param extensions: list of classes with an ``encode()`` method that
            transform the frame and return a new frame; extensions are applied
            in order
        :raises ~websockets.exceptions.ProtocolError: if the frame
            contains incorrect values

        """
        # The frame is written in a single call to write in order to prevent
        # TCP fragmentation. See #68 for details. This also makes it safe to
        # send frames concurrently from multiple coroutines.
        write(self.new_frame.serialize(mask=mask, extensions=extensions))


# Backwards compatibility with previously documented public APIs

from ..frames import Close, prepare_ctrl as encode_data, prepare_data  # noqa


def parse_close(data: bytes) -> Tuple[int, str]:
    """
    Parse the payload from a close frame.

    Return ``(code, reason)``.

    :raises ~websockets.exceptions.ProtocolError: if data is ill-formed
    :raises UnicodeDecodeError: if the reason isn't valid UTF-8

    """
    return dataclasses.astuple(Close.parse(data))  # type: ignore


def serialize_close(code: int, reason: str) -> bytes:
    """
    Serialize the payload for a close frame.

    This is the reverse of :func:`parse_close`.

    """
    return Close(code, reason).serialize()
