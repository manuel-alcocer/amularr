"""Wire encoding of EC packets and tags.

Packet layout (all integers big endian unless the UTF-8 numbers flag is
set, in which case every fixed-size integer field is written as a
UTF-8-style variable-length sequence):

    uint32 flags            (never UTF-8 encoded)
    uint32 body_length      (never UTF-8 encoded; length of the body as
                             transmitted, i.e. after zlib if used)
    body:
        uint8  opcode
        uint16 child_count  (0xFFFF + uint32 when LARGE_TAG_COUNT is on)
        tag * child_count

Tag layout:

    uint16 name_and_flag    (name << 1 | has_children)
    uint8  type
    uint32 length           (logical length: data + serialized children,
                             counted with fixed-size integers regardless
                             of the UTF-8 numbers flag)
    [uint16 child_count, tag * child_count]   if has_children
    bytes  data[length - children_logical_length]
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Iterator

from . import codes as C


class ECProtocolError(Exception):
    """Raised on malformed frames."""


# --------------------------------------------------------------------------
# Tag model
# --------------------------------------------------------------------------


@dataclass
class Tag:
    name: int
    type: int = C.TAGTYPE_UNKNOWN
    data: bytes = b""
    children: list["Tag"] = field(default_factory=list)

    # -- constructors -------------------------------------------------------

    @classmethod
    def empty(cls, name: int) -> "Tag":
        return cls(name)

    @classmethod
    def uint(cls, name: int, value: int) -> "Tag":
        """Smallest unsigned integer type that fits, like CECTag::InitInt."""
        if value < 0:
            raise ValueError("EC integers are unsigned")
        if value <= 0xFF:
            return cls(name, C.TAGTYPE_UINT8, struct.pack(">B", value))
        if value <= 0xFFFF:
            return cls(name, C.TAGTYPE_UINT16, struct.pack(">H", value))
        if value <= 0xFFFFFFFF:
            return cls(name, C.TAGTYPE_UINT32, struct.pack(">I", value))
        return cls(name, C.TAGTYPE_UINT64, struct.pack(">Q", value))

    @classmethod
    def string(cls, name: int, value: str) -> "Tag":
        return cls(name, C.TAGTYPE_STRING, value.encode("utf-8") + b"\x00")

    @classmethod
    def hash16(cls, name: int, value: bytes | str) -> "Tag":
        if isinstance(value, str):
            value = bytes.fromhex(value)
        if len(value) != 16:
            raise ValueError("MD4 hash must be 16 bytes")
        return cls(name, C.TAGTYPE_HASH16, bytes(value))

    # -- accessors ----------------------------------------------------------

    def add(self, child: "Tag") -> "Tag":
        self.children.append(child)
        return self

    def find(self, name: int) -> "Tag | None":
        for child in self.children:
            if child.name == name:
                return child
        return None

    def find_all(self, name: int) -> Iterator["Tag"]:
        return (c for c in self.children if c.name == name)

    def get_int(self, name: int | None = None, default: int = 0) -> int:
        tag = self if name is None else self.find(name)
        if tag is None or not tag.is_int:
            return default
        return int.from_bytes(tag.data, "big")

    def get_str(self, name: int | None = None, default: str = "") -> str:
        tag = self if name is None else self.find(name)
        if tag is None or tag.type != C.TAGTYPE_STRING:
            return default
        return tag.data.rstrip(b"\x00").decode("utf-8", "replace")

    def get_hash(self, name: int | None = None) -> str:
        tag = self if name is None else self.find(name)
        if tag is None or tag.type != C.TAGTYPE_HASH16:
            return ""
        return tag.data.hex().upper()

    @property
    def is_int(self) -> bool:
        return C.TAGTYPE_UINT8 <= self.type <= C.TAGTYPE_UINT64

    @property
    def value(self):
        if self.is_int:
            return self.get_int()
        if self.type == C.TAGTYPE_STRING:
            return self.get_str()
        if self.type == C.TAGTYPE_HASH16:
            return self.get_hash()
        return self.data

    # -- logical length (matches CECTag::GetTagLen) -------------------------

    def logical_len(self, large_count: bool) -> int:
        length = len(self.data)
        for child in self.children:
            length += child.logical_len(large_count) + 2 + 1 + 4
            if child.children:
                length += 2
                if large_count and len(child.children) >= 0xFFFF:
                    length += 4
        return length

    def dump(self, indent: int = 0) -> str:
        pad = "  " * indent
        name = C.TAG_NAMES.get(self.name, hex(self.name))
        out = f"{pad}{name} ({self.type}) = {self.value!r}\n"
        for child in self.children:
            out += child.dump(indent + 1)
        return out


@dataclass
class Packet:
    opcode: int
    children: list[Tag] = field(default_factory=list)

    @classmethod
    def new(cls, opcode: int, detail: int = C.DETAIL_FULL) -> "Packet":
        packet = cls(opcode)
        if detail != C.DETAIL_FULL:
            packet.add(Tag.uint(C.TAG_DETAIL_LEVEL, detail))
        return packet

    def add(self, tag: Tag) -> "Packet":
        self.children.append(tag)
        return self

    def find(self, name: int) -> Tag | None:
        for child in self.children:
            if child.name == name:
                return child
        return None

    def find_all(self, name: int) -> Iterator[Tag]:
        return (c for c in self.children if c.name == name)

    def get_str(self, name: int, default: str = "") -> str:
        tag = self.find(name)
        return tag.get_str() if tag is not None else default

    def get_int(self, name: int, default: int = 0) -> int:
        tag = self.find(name)
        return tag.get_int() if tag is not None else default

    def dump(self) -> str:
        name = C.OP_NAMES.get(self.opcode, hex(self.opcode))
        out = f"{name}\n"
        for child in self.children:
            out += child.dump(1)
        return out


# --------------------------------------------------------------------------
# Number encoding helpers
# --------------------------------------------------------------------------


def utf8_encode_number(value: int) -> bytes:
    """aMule's utf8_wctomb: up to 6 bytes, 31 bits."""
    if value < 0x80:
        return bytes([value])
    table = [
        (0xC0, 1, 0x7FF),
        (0xE0, 2, 0xFFFF),
        (0xF0, 3, 0x1FFFFF),
        (0xF8, 4, 0x3FFFFFF),
        (0xFC, 5, 0x7FFFFFFF),
    ]
    for lead, cont, limit in table:
        if value <= limit:
            out = bytearray([lead | (value >> (6 * cont))])
            for i in range(cont - 1, -1, -1):
                out.append(0x80 | ((value >> (6 * i)) & 0x3F))
            return bytes(out)
    raise ValueError("number too large for UTF-8 encoding")


def utf8_remaining_bytes(first: int) -> int:
    if first < 0x80:
        return 0
    if first & 0xE0 == 0xC0:
        return 1
    if first & 0xF0 == 0xE0:
        return 2
    if first & 0xF8 == 0xF0:
        return 3
    if first & 0xFC == 0xF8:
        return 4
    if first & 0xFE == 0xFC:
        return 5
    raise ECProtocolError(f"invalid UTF-8 number lead byte {first:#x}")


# --------------------------------------------------------------------------
# Reader / Writer
# --------------------------------------------------------------------------


class _Reader:
    def __init__(self, buf: bytes, flags: int):
        self.buf = buf
        self.pos = 0
        self.utf8 = bool(flags & C.FLAG_UTF8_NUMBERS)
        self.large = bool(flags & C.FLAG_LARGE_TAG_COUNT)

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise ECProtocolError("truncated packet")
        chunk = self.buf[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def number(self, size: int) -> int:
        if self.utf8:
            first = self.take(1)[0]
            remaining = utf8_remaining_bytes(first)
            if remaining == 0:
                return first
            masks = {1: 0x1F, 2: 0x0F, 3: 0x07, 4: 0x03, 5: 0x01}
            value = first & masks[remaining]
            for byte in self.take(remaining):
                if byte & 0xC0 != 0x80:
                    raise ECProtocolError("invalid UTF-8 continuation byte")
                value = (value << 6) | (byte & 0x3F)
            return value
        return int.from_bytes(self.take(size), "big")

    def child_count(self) -> int:
        count = self.number(2)
        if self.large and count == 0xFFFF:
            count = self.number(4)
        return count

    def tag(self) -> Tag:
        raw_name = self.number(2)
        has_children = bool(raw_name & 1)
        name = raw_name >> 1
        tag_type = self.number(1)
        length = self.number(4)
        tag = Tag(name, tag_type)
        if has_children:
            for _ in range(self.child_count()):
                tag.children.append(self.tag())
        children_len = tag.logical_len(self.large)
        if length < children_len:
            raise ECProtocolError("tag length smaller than its children")
        tag.data = self.take(length - children_len)
        return tag

    def packet(self) -> Packet:
        opcode = self.number(1)
        packet = Packet(opcode)
        for _ in range(self.child_count()):
            packet.children.append(self.tag())
        return packet


class _Writer:
    def __init__(self, flags: int):
        self.out = bytearray()
        self.utf8 = bool(flags & C.FLAG_UTF8_NUMBERS)
        self.large = bool(flags & C.FLAG_LARGE_TAG_COUNT)

    def number(self, value: int, size: int) -> None:
        if self.utf8:
            self.out += utf8_encode_number(value)
        else:
            self.out += value.to_bytes(size, "big")

    def child_count(self, count: int) -> None:
        if self.large and count >= 0xFFFF:
            self.number(0xFFFF, 2)
            self.number(count, 4)
        else:
            if count > 0xFFFE:
                raise ECProtocolError("too many children without LARGE_TAG_COUNT")
            self.number(count, 2)

    def tag(self, tag: Tag) -> None:
        has_children = bool(tag.children)
        self.number((tag.name << 1) | int(has_children), 2)
        self.number(tag.type, 1)
        self.number(tag.logical_len(self.large), 4)
        if has_children:
            self.child_count(len(tag.children))
            for child in tag.children:
                self.tag(child)
        self.out += tag.data

    def packet(self, packet: Packet) -> bytes:
        self.number(packet.opcode, 1)
        self.child_count(len(packet.children))
        for child in packet.children:
            self.tag(child)
        return bytes(self.out)


# --------------------------------------------------------------------------
# Public frame API
# --------------------------------------------------------------------------


HEADER = struct.Struct(">II")


def encode_frame(packet: Packet, flags: int = C.FLAG_BASE) -> bytes:
    """Serialize a packet into a complete frame (header + body)."""
    body = _Writer(flags).packet(packet)
    if flags & C.FLAG_ZLIB:
        body = zlib.compress(body)
    return HEADER.pack(flags, len(body)) + body


def decode_body(body: bytes, flags: int) -> Packet:
    """Parse a frame body whose header has already been consumed."""
    if (flags & 0x60) != 0x20 or (flags & C.FLAG_UNKNOWN_MASK):
        raise ECProtocolError(f"invalid packet flags {flags:#x}")
    if flags & C.FLAG_ZLIB:
        body = zlib.decompressobj().decompress(body)
    reader = _Reader(body, flags)
    packet = reader.packet()
    return packet
