import struct
import zlib

import pytest

from amularr.ec import codes as C
from amularr.ec.tag import (
    HEADER,
    ECProtocolError,
    Packet,
    Tag,
    decode_body,
    encode_frame,
    utf8_encode_number,
)


def roundtrip(packet: Packet, flags: int) -> Packet:
    frame = encode_frame(packet, flags)
    got_flags, length = HEADER.unpack(frame[: HEADER.size])
    assert got_flags == flags
    body = frame[HEADER.size :]
    assert len(body) == length
    return decode_body(body, got_flags)


def sample_packet() -> Packet:
    packet = Packet.new(C.OP_SEARCH_RESULTS, C.DETAIL_CMD)
    result = Tag.uint(C.TAG_SEARCHFILE, 0x12345)
    result.add(Tag.string(C.TAG_PARTFILE_NAME, "Ubuntú 24.04 — desktop.iso"))
    result.add(Tag.uint(C.TAG_PARTFILE_SIZE_FULL, 5_000_000_000))
    result.add(Tag.hash16(C.TAG_PARTFILE_HASH, "0123456789ABCDEF0123456789ABCDEF"))
    result.add(Tag.uint(C.TAG_PARTFILE_SOURCE_COUNT, 300))
    result.add(Tag.empty(C.TAG_CAN_ZLIB))
    packet.add(result)
    packet.add(Tag.string(C.TAG_STRING, ""))
    return packet


@pytest.mark.parametrize(
    "flags",
    [
        C.FLAG_BASE,
        C.FLAG_BASE | C.FLAG_UTF8_NUMBERS,
        C.FLAG_BASE | C.FLAG_ZLIB,
        C.FLAG_BASE | C.FLAG_UTF8_NUMBERS | C.FLAG_LARGE_TAG_COUNT,
        C.FLAG_BASE | C.FLAG_ZLIB | C.FLAG_LARGE_TAG_COUNT,
    ],
)
def test_roundtrip_all_flag_combinations(flags):
    packet = sample_packet()
    decoded = roundtrip(packet, flags)
    assert decoded.opcode == C.OP_SEARCH_RESULTS
    assert decoded.get_int(C.TAG_DETAIL_LEVEL) == C.DETAIL_CMD
    result = decoded.find(C.TAG_SEARCHFILE)
    assert result.get_int() == 0x12345
    assert result.get_str(C.TAG_PARTFILE_NAME) == "Ubuntú 24.04 — desktop.iso"
    assert result.get_int(C.TAG_PARTFILE_SIZE_FULL) == 5_000_000_000
    assert result.get_hash(C.TAG_PARTFILE_HASH) == "0123456789ABCDEF0123456789ABCDEF"
    assert result.get_int(C.TAG_PARTFILE_SOURCE_COUNT) == 300
    assert result.find(C.TAG_CAN_ZLIB).type == C.TAGTYPE_UNKNOWN
    assert decoded.get_str(C.TAG_STRING) == ""


def test_int_uses_smallest_type():
    assert Tag.uint(1, 0).type == C.TAGTYPE_UINT8
    assert Tag.uint(1, 255).type == C.TAGTYPE_UINT8
    assert Tag.uint(1, 256).type == C.TAGTYPE_UINT16
    assert Tag.uint(1, 0x10000).type == C.TAGTYPE_UINT32
    assert Tag.uint(1, 0x100000000).type == C.TAGTYPE_UINT64


def test_plain_wire_bytes_match_amule_layout():
    """Hand-check the byte layout of a minimal packet with one string tag."""
    packet = Packet(C.OP_ADD_LINK).add(Tag.string(C.TAG_STRING, "ab"))
    frame = encode_frame(packet, C.FLAG_BASE)
    body = frame[HEADER.size :]
    expected = (
        bytes([C.OP_ADD_LINK])
        + struct.pack(">H", 1)  # child count
        + struct.pack(">H", C.TAG_STRING << 1)  # name, no children
        + bytes([C.TAGTYPE_STRING])
        + struct.pack(">I", 3)  # "ab\0"
        + b"ab\x00"
    )
    assert body == expected


def test_nested_tag_length_is_logical_under_utf8():
    """Tag length counts children with fixed-size headers even when the
    wire uses UTF-8 numbers (mirrors CECTag::GetTagLen)."""
    parent = Tag.uint(C.TAG_PARTFILE, 1)
    parent.add(Tag.uint(C.TAG_PARTFILE_CAT, 0x1234))
    assert parent.logical_len(False) == 1 + (2 + 7)
    packet = Packet(C.OP_DOWNLOAD_SEARCH_RESULT).add(parent)
    decoded = roundtrip(packet, C.FLAG_BASE | C.FLAG_UTF8_NUMBERS)
    got = decoded.find(C.TAG_PARTFILE)
    assert got.get_int() == 1
    assert got.get_int(C.TAG_PARTFILE_CAT) == 0x1234


@pytest.mark.parametrize("value", [0, 1, 0x7F, 0x80, 0x7FF, 0x800, 0xFFFF, 0x10000, 0x1FFFFF, 0x3FFFFFF, 0x7FFFFFFF])
def test_utf8_number_encoding_roundtrip(value):
    from amularr.ec.tag import _Reader

    encoded = utf8_encode_number(value)
    reader = _Reader(encoded, C.FLAG_UTF8_NUMBERS)
    assert reader.number(4) == value
    assert reader.pos == len(encoded)


def test_utf8_number_encoding_matches_standard_utf8_for_small_values():
    for value in (0x41, 0xE9, 0x20AC, 0x1F600):
        assert utf8_encode_number(value) == chr(value).encode("utf-8")


def test_large_tag_count_sentinel():
    packet = Packet(C.OP_SEARCH_RESULTS)
    for i in range(0x10001):
        packet.add(Tag.uint(C.TAG_SEARCHFILE, i))
    flags = C.FLAG_BASE | C.FLAG_ZLIB | C.FLAG_LARGE_TAG_COUNT
    decoded = roundtrip(packet, flags)
    assert len(decoded.children) == 0x10001
    assert decoded.children[-1].get_int() == 0x10000


def test_too_many_children_without_large_count_is_refused():
    packet = Packet(C.OP_SEARCH_RESULTS)
    for i in range(0xFFFF):
        packet.add(Tag.empty(C.TAG_SEARCHFILE))
    with pytest.raises(ECProtocolError):
        encode_frame(packet, C.FLAG_BASE)


def test_invalid_flags_rejected():
    with pytest.raises(ECProtocolError):
        decode_body(b"\x01\x00\x00", 0x00)
    with pytest.raises(ECProtocolError):
        decode_body(b"\x01\x00\x00", 0x20 | 0x08)


def test_truncated_body_rejected():
    body = encode_frame(sample_packet(), C.FLAG_BASE)[HEADER.size :]
    with pytest.raises(ECProtocolError):
        decode_body(body[:-5], C.FLAG_BASE)


def test_zlib_body_is_actually_compressed():
    packet = Packet(C.OP_STRINGS).add(Tag.string(C.TAG_STRING, "x" * 5000))
    frame = encode_frame(packet, C.FLAG_BASE | C.FLAG_ZLIB)
    assert len(frame) < 1000
    body = frame[HEADER.size :]
    assert zlib.decompress(body)[0] == C.OP_STRINGS
