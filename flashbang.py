#!/usr/bin/env python3
"""
Flashbang - a controlled corruption engine for Flash (.swf) games.

Corrupts SWF files in a structure-aware way so the result still runs.
Instead of flipping random bytes anywhere (which just makes Flash Player
refuse to load the file), Flashbang parses the tag stream and only damages
byte ranges that are known to be non-fatal:

  * tag headers, tag lengths and character IDs are never touched
  * the SWF header stays byte-identical
  * every corruption is length-preserving, so nothing shifts
  * class-binding tags (SymbolClass / ExportAssets) are left alone
  * JPEG / MP3 byte-stuffing rules are respected so decoders keep decoding

Targets: graphics, sound, logic (or all of them at once), each with its
own strength.

    flashbang.py game.swf -o out.swf -t all -s 35
    flashbang.py game.swf --report
    flashbang.py game.swf -t graphics,sound -s graphics=60,sound=15

Requires nothing but the Python standard library.
"""

import argparse
import os
import random
import re
import struct
import sys
import zlib
import lzma

VERSION = "1.0"

# ---------------------------------------------------------------------------
# tag tables
# ---------------------------------------------------------------------------

TAG_NAMES = {
    0: "End", 1: "ShowFrame", 2: "DefineShape", 4: "PlaceObject",
    5: "RemoveObject", 6: "DefineBits", 7: "DefineButton", 8: "JPEGTables",
    9: "SetBackgroundColor", 10: "DefineFont", 11: "DefineText",
    12: "DoAction", 13: "DefineFontInfo", 14: "DefineSound",
    15: "StartSound", 17: "DefineButtonSound", 18: "SoundStreamHead",
    19: "SoundStreamBlock", 20: "DefineBitsLossless",
    21: "DefineBitsJPEG2", 22: "DefineShape2", 24: "Protect",
    26: "PlaceObject2", 28: "RemoveObject2", 32: "DefineShape3",
    33: "DefineText2", 34: "DefineButton2", 35: "DefineBitsJPEG3",
    36: "DefineBitsLossless2", 37: "DefineEditText", 39: "DefineSprite",
    43: "FrameLabel", 45: "SoundStreamHead2", 46: "DefineMorphShape",
    48: "DefineFont2", 56: "ExportAssets", 57: "ImportAssets",
    58: "EnableDebugger", 59: "DoInitAction", 60: "DefineVideoStream",
    61: "VideoFrame", 62: "DefineFontInfo2", 64: "EnableDebugger2",
    65: "ScriptLimits", 66: "SetTabIndex", 69: "FileAttributes",
    70: "PlaceObject3", 71: "ImportAssets2", 72: "DoABC1",
    73: "DefineFontAlignZones", 74: "CSMTextSettings", 75: "DefineFont3",
    76: "SymbolClass", 77: "Metadata", 78: "DefineScalingGrid",
    82: "DoABC", 83: "DefineShape4", 84: "DefineMorphShape2",
    86: "DefineSceneAndFrameLabelData", 87: "DefineBinaryData",
    88: "DefineFontName", 89: "StartSound2", 90: "DefineBitsJPEG4",
    91: "DefineFont4",
}

# tags that must never be modified - they carry structure, linkage or
# metadata that Flash Player validates before anything else runs
UNTOUCHABLE = {
    0, 1, 5, 8, 24, 28, 43, 56, 57, 58, 64, 65, 66, 69, 71,
    73, 74, 76, 77, 78, 86, 88, 91,
}

SHAPE_TAGS = {2, 22, 32, 83}
MORPH_TAGS = {46, 84}
TEXT_TAGS = {11, 33, 37}
FONT_TAGS = {10, 48, 75}
BUTTON_TAGS = {7, 34}
JPEG_TAGS = {6, 21, 35, 90}
LOSSLESS_TAGS = {20, 36}
PLACE_TAGS = {26, 70}
VIDEO_TAGS = {60, 61}

SOUND_DEFINE = {14}
SOUND_STREAM_HEAD = {18, 45}
SOUND_STREAM_BLOCK = {19}

ACTION_TAGS = {12, 59}
ABC_TAGS = {72, 82}

# how many bytes at the start of a tag body are the character ID
HAS_CHARACTER_ID = (
    SHAPE_TAGS | MORPH_TAGS | TEXT_TAGS | FONT_TAGS | BUTTON_TAGS
    | JPEG_TAGS | LOSSLESS_TAGS | VIDEO_TAGS | SOUND_DEFINE | {39}
)

IDENTIFIER_RE = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*"
    r"(?:[.:][A-Za-z_$][A-Za-z0-9_$]*)*$"
)


def looks_like_code(s):
    """True if a string is probably a symbol name rather than display text.

    Corrupting property/class/method names breaks name lookup and takes the
    whole movie down, so those are skipped. Anything with spaces, punctuation
    or sentence structure is fair game.
    """
    if len(s) < 3:
        return True
    if IDENTIFIER_RE.match(s):
        return True
    if s.startswith("http") or s.startswith("/") or s.startswith("_"):
        return True
    return False


# ---------------------------------------------------------------------------
# container handling
# ---------------------------------------------------------------------------

class Swf:
    """An unpacked SWF: 8-byte header plus a mutable, decompressed body."""

    def __init__(self, signature, version, file_length, body):
        self.signature = signature      # b'FWS' / b'CWS' / b'ZWS'
        self.version = version
        self.file_length = file_length
        self.body = bytearray(body)     # everything after byte 8

    @classmethod
    def load(cls, path):
        with open(path, "rb") as fh:
            raw = fh.read()
        if len(raw) < 8:
            raise ValueError("file is too short to be a SWF")
        sig = bytes(raw[0:3])
        version = raw[3]
        file_length = struct.unpack_from("<I", raw, 4)[0]

        if sig == b"FWS":
            body = raw[8:]
        elif sig == b"CWS":
            body = zlib.decompress(raw[8:])
        elif sig == b"ZWS":
            props = raw[12:17]
            payload = raw[17:]
            expected = max(file_length - 8, 0)
            dec = lzma.LZMADecompressor(
                format=lzma.FORMAT_ALONE,
                filters=None,
            )
            header = props + struct.pack("<Q", 0xFFFFFFFFFFFFFFFF)
            body = dec.decompress(header + payload, expected or -1)
        else:
            raise ValueError(
                "not a SWF (signature %r) - Flashbang only eats .swf" % sig
            )
        return cls(sig, version, file_length, body)

    def save(self, path, compress=None):
        if compress is None:
            compress = self.signature in (b"CWS", b"ZWS")
        body = bytes(self.body)
        total = len(body) + 8
        if compress and self.version >= 6:
            sig = b"CWS"
            payload = zlib.compress(body, 9)
        else:
            sig = b"FWS"
            payload = body
        out = bytearray()
        out += sig
        out.append(self.version)
        out += struct.pack("<I", total)
        out += payload
        with open(path, "wb") as fh:
            fh.write(out)
        return len(out)


# ---------------------------------------------------------------------------
# bit-level reader (SWF packs RECT / MATRIX / CXFORM as bit fields)
# ---------------------------------------------------------------------------

class Bits:
    def __init__(self, buf, start_byte):
        self.buf = buf
        self.pos = start_byte * 8

    def read(self, n):
        v = 0
        for _ in range(n):
            byte = self.buf[self.pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def align(self):
        self.pos = (self.pos + 7) & ~7

    @property
    def byte_pos(self):
        return (self.pos + 7) // 8


def skip_rect(bits):
    nbits = bits.read(5)
    bits.read(nbits * 4)


def parse_matrix(bits):
    """Read a MATRIX, returning (bit_offset, bit_count) spans for its values."""
    spans = []
    if bits.read(1):                     # HasScale
        nb = bits.read(5)
        if nb:
            spans.append((bits.pos, nb)); bits.read(nb)
            spans.append((bits.pos, nb)); bits.read(nb)
    if bits.read(1):                     # HasRotate
        nb = bits.read(5)
        if nb:
            spans.append((bits.pos, nb)); bits.read(nb)
            spans.append((bits.pos, nb)); bits.read(nb)
    nb = bits.read(5)                    # translate
    if nb:
        spans.append((bits.pos, nb)); bits.read(nb)
        spans.append((bits.pos, nb)); bits.read(nb)
    bits.align()
    return spans


def parse_cxform(bits, with_alpha):
    spans = []
    has_add = bits.read(1)
    has_mult = bits.read(1)
    nb = bits.read(4)
    count = 4 if with_alpha else 3
    for _ in range(2):
        pass
    if has_mult and nb:
        for _ in range(count):
            spans.append((bits.pos, nb)); bits.read(nb)
    if has_add and nb:
        for _ in range(count):
            spans.append((bits.pos, nb)); bits.read(nb)
    bits.align()
    return spans


# ---------------------------------------------------------------------------
# tag walking
# ---------------------------------------------------------------------------

def header_body_start(body):
    """Byte offset of the first tag (past RECT + framerate + framecount)."""
    bits = Bits(body, 0)
    skip_rect(bits)
    bits.align()
    return bits.byte_pos + 4


def iter_tags(body, start, end):
    pos = start
    while pos + 2 <= end:
        code_len = struct.unpack_from("<H", body, pos)[0]
        code = code_len >> 6
        length = code_len & 0x3F
        hdr = 2
        if length == 0x3F:
            if pos + 6 > end:
                return
            length = struct.unpack_from("<I", body, pos + 2)[0]
            hdr = 6
        b0 = pos + hdr
        b1 = b0 + length
        if b1 > end:
            return
        yield code, b0, b1
        if code == 0:
            return
        pos = b1


# ---------------------------------------------------------------------------
# region model
# ---------------------------------------------------------------------------

class Region:
    """A byte or bit range that may be corrupted, plus how to corrupt it."""

    __slots__ = ("kind", "target", "start", "end", "spans", "meta")

    def __init__(self, kind, target, start=0, end=0, spans=None, meta=None):
        self.kind = kind        # 'bytes' | 'jpeg' | 'mp3' | 'bits' | 'zlib'
        self.target = target    # 'graphics' | 'sound' | 'logic'
        self.start = start
        self.end = end
        self.spans = spans or []
        self.meta = meta or {}

    @property
    def size(self):
        if self.kind == "bits":
            return sum(n for _, n in self.spans) // 8 + 1
        return self.end - self.start


# ---------------------------------------------------------------------------
# scanner - decides what is safe to break
# ---------------------------------------------------------------------------

class Scanner:
    def __init__(self, swf, targets, wild=False):
        self.swf = swf
        self.body = swf.body
        self.targets = targets
        self.wild = wild
        self.regions = []
        self.tag_counts = {}
        self.stream_format = 2          # assume MP3 streaming audio
        self.skipped = []

    def want(self, target):
        return target in self.targets

    def scan(self):
        start = header_body_start(self.body)
        self._walk(start, len(self.body))
        return self.regions

    def _walk(self, start, end):
        for code, b0, b1 in iter_tags(self.body, start, end):
            self.tag_counts[code] = self.tag_counts.get(code, 0) + 1
            if code == 39:                       # DefineSprite - recurse
                # body: SpriteID u16, FrameCount u16, then nested tags
                if b1 - b0 >= 4:
                    self._walk(b0 + 4, b1)
                continue
            if code in UNTOUCHABLE:
                continue
            try:
                self._tag(code, b0, b1)
            except Exception as exc:             # never let one tag kill a run
                self.skipped.append((code, str(exc)))

    # -- per-tag dispatch ---------------------------------------------------

    def _tag(self, code, b0, b1):
        if code in SOUND_STREAM_HEAD:
            if b1 - b0 >= 2:
                self.stream_format = self.body[b0 + 1] >> 4
            return

        if self.want("graphics"):
            if code in SHAPE_TAGS:
                return self._shape(code, b0, b1)
            if code in MORPH_TAGS or code in TEXT_TAGS or code in FONT_TAGS \
                    or code in BUTTON_TAGS or code in VIDEO_TAGS:
                return self._generic_character(b0, b1)
            if code in JPEG_TAGS:
                return self._jpeg(code, b0, b1)
            if code in LOSSLESS_TAGS:
                return self._lossless(code, b0, b1)
            if code in PLACE_TAGS:
                return self._place(code, b0, b1)
            if code == 9 and self.wild:
                return self._add(Region("bytes", "graphics", b0, b1))
            if code == 87 and self.wild:      # DefineBinaryData
                return self._add(Region("bytes", "graphics", b0 + 6, b1))
        if code == 87:
            return

        if self.want("sound"):
            if code in SOUND_DEFINE:
                return self._define_sound(b0, b1)
            if code in SOUND_STREAM_BLOCK:
                return self._stream_block(b0, b1)

        if self.want("logic"):
            if code in ACTION_TAGS:
                return self._do_action(code, b0, b1)
            if code in ABC_TAGS:
                return self._do_abc(code, b0, b1)

    def _add(self, region):
        if region.size > 0 or region.spans:
            self.regions.append(region)

    # -- graphics -----------------------------------------------------------

    def _shape(self, code, b0, b1):
        bits = Bits(self.body, b0 + 2)          # skip ShapeId
        skip_rect(bits)
        bits.align()
        pos = bits.byte_pos
        if code == 83:                          # DefineShape4: EdgeBounds+flags
            b = Bits(self.body, pos)
            skip_rect(b)
            b.align()
            pos = b.byte_pos + 1
        if self.wild:
            pos = b0 + 2                        # let the bounds get hit too
        self._add(Region("bytes", "graphics", pos, b1))

    def _generic_character(self, b0, b1):
        self._add(Region("bytes", "graphics", b0 + 2, b1))

    def _jpeg(self, code, b0, b1):
        pos = b0
        if code in (6, 21, 35, 90):
            pos = b0 + 2                        # CharacterID
        alpha_start = None
        if code in (35, 90):
            extra = 4 if code == 35 else 5      # AlphaDataOffset (+DeblockParam)
            if code == 90:
                data_len = struct.unpack_from("<I", self.body, pos)[0]
                pos += 6
            else:
                data_len = struct.unpack_from("<I", self.body, pos)[0]
                pos += 4
            alpha_start = pos + data_len
            jpeg_end = min(alpha_start, b1)
        else:
            jpeg_end = b1
        scan = self._find_scan(pos, jpeg_end)
        if scan is not None:
            self._add(Region("jpeg", "graphics", scan, jpeg_end))
        if alpha_start is not None and alpha_start < b1:
            self._add(Region("zlib", "graphics", alpha_start, b1))

    def _find_scan(self, start, end):
        """Locate the start of JPEG entropy-coded data (after the last SOS)."""
        i = start
        found = None
        while i + 3 < end:
            if self.body[i] == 0xFF:
                marker = self.body[i + 1]
                if marker == 0xDA:
                    seg = struct.unpack_from(">H", self.body, i + 2)[0]
                    found = i + 2 + seg
                    i = found
                    continue
                if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                if marker == 0xFF:
                    i += 1
                    continue
                seg = struct.unpack_from(">H", self.body, i + 2)[0]
                i += 2 + seg
                continue
            i += 1
        return found

    def _lossless(self, code, b0, b1):
        pos = b0 + 2                            # CharacterID
        fmt = self.body[pos]
        pos += 5                                # format + width + height
        if fmt == 3:
            pos += 1                            # ColorTableSize
        if pos < b1:
            self._add(Region("zlib", "graphics", pos, b1))

    def _place(self, code, b0, b1):
        bits = Bits(self.body, b0)
        f1 = bits.read(8)
        f2 = 0
        if code == 70:
            f2 = bits.read(8)
        has_clip_actions = bool(f1 & 0x80)
        has_clip_depth = bool(f1 & 0x40)
        has_name = bool(f1 & 0x20)
        has_ratio = bool(f1 & 0x10)
        has_cxform = bool(f1 & 0x08)
        has_matrix = bool(f1 & 0x04)
        has_char = bool(f1 & 0x02)
        bits.read(16)                           # Depth
        if code == 70:
            has_class = bool(f2 & 0x08)
            has_image = bool(f2 & 0x10)
            if has_class or (has_image and has_char):
                while self.body[bits.byte_pos] != 0:
                    bits.pos += 8
                bits.pos += 8
        if has_char:
            bits.read(16)                       # CharacterId - never touched
        spans = []
        if has_matrix:
            spans += parse_matrix(bits)
        if has_cxform:
            spans += parse_cxform(bits, with_alpha=True)
        if spans:
            self._add(Region("bits", "graphics", b0, b1, spans=spans))
        _ = (has_clip_actions, has_clip_depth, has_name, has_ratio)

    # -- sound --------------------------------------------------------------

    def _define_sound(self, b0, b1):
        flags = self.body[b0 + 2]
        fmt = flags >> 4
        pos = b0 + 7                            # id(2) + flags(1) + count(4)
        if fmt == 2:                            # MP3 has a SeekSamples i16
            pos += 2
            kind = "mp3"
        elif fmt == 6:                          # Nellymoser / Speex - fragile
            kind = "mp3"
        else:
            kind = "bytes"
        if pos < b1:
            self._add(Region(kind, "sound", pos, b1))

    def _stream_block(self, b0, b1):
        if self.stream_format == 2:
            pos = b0 + 4                        # SampleCount + SeekSamples
            kind = "mp3"
        else:
            pos = b0
            kind = "bytes"
        if pos < b1:
            self._add(Region(kind, "sound", pos, b1))

    # -- logic (ActionScript 2) --------------------------------------------

    def _do_action(self, code, b0, b1):
        pos = b0
        if code == 59:                          # DoInitAction: SpriteID first
            pos += 2
        spans = []
        while pos < b1:
            op = self.body[pos]
            if op < 0x80:
                pos += 1
                continue
            if pos + 3 > b1:
                break
            length = struct.unpack_from("<H", self.body, pos + 1)[0]
            data0 = pos + 3
            data1 = min(data0 + length, b1)
            if op == 0x96:                      # ActionPush
                spans += self._push_spans(data0, data1)
            elif op == 0x88:                    # ActionConstantPool
                spans += self._pool_spans(data0, data1)
            pos = data1
        for s, e in spans:
            self._add(Region("bytes", "logic", s, e))

    def _push_spans(self, s, e):
        out = []
        pos = s
        while pos < e:
            t = self.body[pos]
            pos += 1
            if t == 0:                          # null-terminated string
                end = self.body.find(b"\x00", pos, e)
                if end < 0:
                    break
                text = bytes(self.body[pos:end])
                if not looks_like_code(text.decode("latin-1")):
                    out.append((pos, end))
                pos = end + 1
            elif t == 1:                        # float32 - mantissa only
                out.append((pos, pos + 3))
                pos += 4
            elif t in (2, 3):
                pass
            elif t in (4, 5):                   # register / boolean
                if t == 5:
                    out.append((pos, pos + 1))
                pos += 1
            elif t == 6:                        # double - mantissa only
                out.append((pos, pos + 6))
                pos += 8
            elif t == 7:                        # int32
                out.append((pos, pos + 4))
                pos += 4
            elif t == 8:
                pos += 1                        # constant index - unsafe
            elif t == 9:
                pos += 2                        # constant index - unsafe
            else:
                break
        return out

    def _pool_spans(self, s, e):
        out = []
        if s + 2 > e:
            return out
        pos = s + 2
        while pos < e:
            end = self.body.find(b"\x00", pos, e)
            if end < 0:
                break
            text = bytes(self.body[pos:end])
            if not looks_like_code(text.decode("latin-1")):
                out.append((pos, end))
            pos = end + 1
        return out

    # -- logic (ActionScript 3 / ABC) --------------------------------------

    def _do_abc(self, code, b0, b1):
        pos = b0
        if code == 82:
            pos += 4                            # Flags
            end = self.body.find(b"\x00", pos, b1)
            if end < 0:
                return
            pos = end + 1
        pos += 4                                # minor + major version

        def u30(p):
            val = 0
            for i in range(5):
                byte = self.body[p]
                p += 1
                val |= (byte & 0x7F) << (7 * i)
                if not byte & 0x80:
                    break
            return val, p

        n, pos = u30(pos)                       # int pool
        for _ in range(max(n - 1, 0)):
            _, pos = u30(pos)
        n, pos = u30(pos)                       # uint pool
        for _ in range(max(n - 1, 0)):
            _, pos = u30(pos)

        n, pos = u30(pos)                       # double pool
        for _ in range(max(n - 1, 0)):
            # little-endian float64: corrupt mantissa bytes only, never the
            # exponent - a stray Infinity turns into a hung loop
            self._add(Region("bytes", "logic", pos, pos + 6))
            pos += 8

        n, pos = u30(pos)                       # string pool
        for _ in range(max(n - 1, 0)):
            size, pos = u30(pos)
            if pos + size > b1:
                return
            if size >= 3:
                text = bytes(self.body[pos:pos + size])
                try:
                    decoded = text.decode("utf-8")
                except UnicodeDecodeError:
                    decoded = None
                if decoded is not None and not looks_like_code(decoded):
                    self._add(Region("ascii", "logic", pos, pos + size))
            pos += size


# ---------------------------------------------------------------------------
# corruption
# ---------------------------------------------------------------------------

def strength_to_p(strength, target):
    """Map a 0-100 dial to a per-byte corruption probability."""
    s = max(0.0, min(100.0, float(strength)))
    if s == 0:
        return 0.0
    # geometric ramp: 1 = barely perceptible, 50 = clearly glitched,
    # 100 = the asset is basically gone
    p = 2e-5 * (7500.0 ** ((s - 1) / 99.0))
    if target == "logic":
        # logic regions are tiny (a few bytes per constant), so the same dial
        # position needs a much higher per-byte rate to feel equivalent
        return min(p * 8.0, 0.35)
    return min(p, 0.9)


class Corruptor:
    def __init__(self, body, rng, strengths, wild=False):
        self.body = body
        self.rng = rng
        self.strengths = strengths
        self.wild = wild
        self.stats = {}

    def hit(self, target, n=1):
        self.stats[target] = self.stats.get(target, 0) + n

    def run(self, regions):
        for r in regions:
            p = strength_to_p(self.strengths.get(r.target, 0), r.target)
            if p <= 0:
                continue
            fn = getattr(self, "_do_" + r.kind)
            fn(r, p)
        return self.stats

    # -- strategies ---------------------------------------------------------

    def _do_bytes(self, r, p):
        rng = self.rng
        for i in range(r.start, min(r.end, len(self.body))):
            if rng.random() < p:
                self.body[i] = rng.randrange(256)
                self.hit(r.target)

    def _do_ascii(self, r, p):
        """Scramble printable characters only - keeps UTF-8 valid."""
        rng = self.rng
        for i in range(r.start, min(r.end, len(self.body))):
            b = self.body[i]
            if 0x20 <= b <= 0x7E and rng.random() < p:
                self.body[i] = rng.randrange(0x21, 0x7F)
                self.hit(r.target)

    def _do_jpeg(self, r, p):
        """Damage entropy-coded data without breaking FF byte stuffing."""
        rng = self.rng
        end = min(r.end, len(self.body))
        for i in range(r.start, end):
            if self.body[i] == 0xFF:
                continue
            if i > 0 and self.body[i - 1] == 0xFF:
                continue
            if rng.random() < p:
                self.body[i] = rng.randrange(0x00, 0xFF)   # never emit 0xFF
                self.hit(r.target)

    def _do_mp3(self, r, p):
        """Same idea for MP3: never destroy or fabricate a frame sync."""
        rng = self.rng
        end = min(r.end, len(self.body))
        for i in range(r.start, end):
            if self.body[i] == 0xFF:
                continue
            if i >= 3 and 0xFF in self.body[max(r.start, i - 3):i]:
                continue
            if rng.random() < p:
                self.body[i] = rng.randrange(0x00, 0xFF)
                self.hit(r.target)

    def _do_bits(self, r, p):
        """Flip individual bits inside matrix / colour-transform values."""
        rng = self.rng
        bit_p = min(p * 40.0, 0.5)
        for bit_start, nbits in r.spans:
            first = 0 if self.wild else 1        # keep the sign bit sane
            for k in range(first, nbits):
                if rng.random() < bit_p:
                    idx = bit_start + k
                    byte_i = idx >> 3
                    if byte_i >= len(self.body):
                        continue
                    self.body[byte_i] ^= 1 << (7 - (idx & 7))
                    self.hit(r.target)

    def _do_zlib(self, r, p):
        """Decompress, corrupt the pixels, recompress back into the same slot.

        If the recompressed stream would not fit, the corruption is retried
        with fewer hits. Length must be preserved or every later tag offset
        would shift.
        """
        rng = self.rng
        end = min(r.end, len(self.body))
        raw_slot = bytes(self.body[r.start:end])
        slot = end - r.start
        try:
            plain = zlib.decompress(raw_slot)
        except zlib.error:
            try:
                d = zlib.decompressobj()
                plain = d.decompress(raw_slot)
            except zlib.error:
                return
        if not plain:
            return
        scale = 1.0
        for _ in range(6):
            data = bytearray(plain)
            hits = 0
            eff = p * scale
            for i in range(len(data)):
                if rng.random() < eff:
                    data[i] = rng.randrange(256)
                    hits += 1
            if hits == 0:
                return                       # nothing changed, leave it alone
            packed = zlib.compress(bytes(data), 9)
            if len(packed) <= slot:
                self.body[r.start:r.start + len(packed)] = packed
                for i in range(r.start + len(packed), end):
                    self.body[i] = 0
                self.hit(r.target, hits)
                return
            scale *= 0.35
        return


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def print_report(swf, scanner, regions):
    total = len(swf.body) + 8
    print("  file        %s, v%d, %s" % (
        swf.signature.decode(), swf.version,
        "compressed" if swf.signature != b"FWS" else "uncompressed"))
    print("  size        %d bytes (%d uncompressed)" % (swf.file_length, total))
    print()
    print("  tags found")
    for code in sorted(scanner.tag_counts, key=lambda c: -scanner.tag_counts[c]):
        name = TAG_NAMES.get(code, "Unknown%d" % code)
        print("    %-32s x%d" % (name, scanner.tag_counts[code]))
    print()
    by_target = {}
    for r in regions:
        cur = by_target.setdefault(r.target, [0, 0])
        cur[0] += 1
        cur[1] += r.size
    print("  corruptible surface")
    if not by_target:
        print("    (nothing - try a different --target)")
    for target in ("graphics", "sound", "logic"):
        if target in by_target:
            count, size = by_target[target]
            pct = 100.0 * size / max(len(swf.body), 1)
            print("    %-10s %6d regions   %9d bytes  (%.1f%% of file)"
                  % (target, count, size, pct))
    if scanner.skipped:
        print()
        print("  %d tag(s) skipped as unparseable (left intact)"
              % len(scanner.skipped))


BANNER = r"""
   ___ _         _   _   _
  | __| |__ _ __| |_| |_| |__  __ _ _ _  __ _
  | _|| / _` (_-< ' \| _ \ _ \/ _` | ' \/ _` |
  |_| |_\__,_/__/_||_|_.__.__/\__,_|_||_\__, |
                                         |___/  v%s
""" % VERSION


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def parse_strengths(spec, targets):
    """Accept either '35' or 'graphics=60,sound=20,logic=5'."""
    out = {t: 0.0 for t in ("graphics", "sound", "logic")}
    spec = str(spec).strip()
    if "=" not in spec:
        val = float(spec)
        for t in targets:
            out[t] = val
        return out
    for part in spec.split(","):
        if not part.strip():
            continue
        key, _, val = part.partition("=")
        key = key.strip().lower()
        if key not in out:
            raise ValueError("unknown target %r" % key)
        out[key] = float(val)
    return out


def corrupt_once(path, out_path, targets, strengths, seed, wild, compress,
                 quiet=False):
    swf = Swf.load(path)
    scanner = Scanner(swf, targets, wild=wild)
    regions = scanner.scan()
    rng = random.Random(seed)
    corruptor = Corruptor(swf.body, rng, strengths, wild=wild)
    stats = corruptor.run(regions)
    written = swf.save(out_path, compress=compress)
    if not quiet:
        hits = sum(stats.values())
        detail = ", ".join("%s %d" % (k, v) for k, v in sorted(stats.items()))
        print("  -> %s  (%d bytes, %d hits%s)" % (
            os.path.basename(out_path), written, hits,
            "; " + detail if detail else ""))
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="flashbang",
        description="Structure-aware corruption for Flash (.swf) games.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  flashbang game.swf --report
  flashbang game.swf -o out.swf -t all -s 30
  flashbang game.swf -o out.swf -t graphics,sound -s graphics=60,sound=15
""")
    ap.add_argument("input", help="input .swf")
    ap.add_argument("-o", "--output", help="output .swf")
    ap.add_argument("-t", "--target", default="all",
                    help="graphics, sound, logic, all (comma separated)")
    ap.add_argument("-s", "--strength", default="25",
                    help="0-100, or per-target like graphics=60,sound=10")
    ap.add_argument("--seed", type=int, default=None,
                    help="RNG seed - same seed gives the same corruption")
    ap.add_argument("--wild", action="store_true",
                    help="unlock riskier regions (bounds, sign bits, bg colour)")
    ap.add_argument("--report", action="store_true",
                    help="analyse only, write nothing")
    ap.add_argument("--compress", choices=("keep", "yes", "no"), default="keep",
                    help="output compression (default: match the input)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not args.quiet:
        print(BANNER)

    raw_targets = [t.strip().lower() for t in args.target.split(",") if t.strip()]
    if "all" in raw_targets:
        targets = {"graphics", "sound", "logic"}
    else:
        targets = set(raw_targets)
    bad = targets - {"graphics", "sound", "logic"}
    if bad:
        ap.error("unknown target(s): %s" % ", ".join(sorted(bad)))

    try:
        swf = Swf.load(args.input)
    except Exception as exc:
        ap.error(str(exc))

    if args.report:
        scanner = Scanner(swf, targets, wild=args.wild)
        print_report(swf, scanner, scanner.scan())
        return 0

    try:
        strengths = parse_strengths(args.strength, targets)
    except ValueError as exc:
        ap.error(str(exc))
    for t in list(strengths):
        if t not in targets:
            strengths[t] = 0.0

    compress = {"keep": None, "yes": True, "no": False}[args.compress]
    seed = args.seed if args.seed is not None else random.randrange(1 << 30)
    stem, ext = os.path.splitext(args.input)
    ext = ext or ".swf"

    if not args.quiet:
        print("  target(s)   %s" % ", ".join(sorted(targets)))
        print("  seed        %d" % seed)
        print()

    out = args.output or "%s_flashbanged%s" % (stem, ext)
    corrupt_once(args.input, out, targets, strengths, seed, args.wild,
                 compress, args.quiet)

    if not args.quiet:
        print()
        print("  done. reuse --seed %d to reproduce this exact run." % seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
