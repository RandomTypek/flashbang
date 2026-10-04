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

VERSION = "1.1"

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
TEXT_TAGS = {11, 33}          # DefineText / DefineText2 (glyph records)
EDIT_TEXT_TAGS = {37}         # DefineEditText (has an initial-text string)
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
    SHAPE_TAGS | MORPH_TAGS | TEXT_TAGS | EDIT_TEXT_TAGS | FONT_TAGS
    | BUTTON_TAGS | JPEG_TAGS | LOSSLESS_TAGS | VIDEO_TAGS | SOUND_DEFINE | {39}
)

# the four corruption targets the tool exposes
ALL_TARGETS = ("graphics", "sound", "logic", "text")

IDENTIFIER_RE = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*"
    r"(?:[.:][A-Za-z_$][A-Za-z0-9_$]*)*$"
)

# ActionScript 2 single-byte operators that can be swapped for a sibling with
# the same stack effect (pop two, push one; or pop one, push one). Each group
# lists interchangeable opcodes; a swap keeps the bytecode length identical.
AS2_OP_GROUPS = [
    [0x0A, 0x0B, 0x0C, 0x0D],   # add, subtract, multiply, divide (old arith)
    [0x0E, 0x0F],               # equals, less
    [0x10, 0x11],               # and, or (logical)
    [0x12],                     # not (unary) - left alone unless paired
    [0x47, 0x48, 0x49, 0x4A],   # add2, less2, equals2, toNumber? (SWF6+) *
    [0x60, 0x61, 0x62, 0x63],   # bitAnd, bitOr, bitXor, bitLShift
    [0x64, 0x65],               # bitRShift, bitURShift
    [0x66, 0x67, 0x68],         # strictEquals, greater, stringGreater
    [0x13, 0x29],               # stringEquals, stringLess
]
# binary arithmetic/compare that is genuinely interchangeable (same arity)
AS2_SWAP = {}
for _grp in ([0x0A, 0x0B, 0x0C, 0x0D],      # + - * /
             [0x0E, 0x67],                   # equals <-> greater (both cmp)
             [0x60, 0x61, 0x62],             # & | ^
             [0x64, 0x65]):                  # >> >>>
    for _op in _grp:
        AS2_SWAP[_op] = [x for x in _grp if x != _op]
# conditional branch sense: there is only ActionIf (0x9D); flipping it needs
# operand rewriting, so instead we swap the comparison that feeds it (above).

# ActionScript 3 (AVM2) opcodes, same idea. These are all single-byte with no
# operands, so a swap is length-safe.
ABC_SWAP = {}
for _grp in ([0xA0, 0xA1, 0xA2, 0xA3, 0xA4],  # add subtract multiply divide modulo
             [0xAB, 0xAD],                     # equals, greaterthan
             [0xAC, 0xAE],                     # strictequals, greaterequals
             [0xAF, 0xB0],                     # lessthan, lessequals
             [0xA8, 0xA9, 0xAA],               # bitand bitor bitxor
             [0xA5, 0xA6, 0xA7],               # lshift rshift urshift
             [0x12, 0x13]):                    # iffalse, iftrue (same operand size)
    for _op in _grp:
        ABC_SWAP[_op] = [x for x in _grp if x != _op]
del _grp, _op


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
        if self.kind in ("bits", "glyph"):
            return sum(n for _, n in self.spans) // 8 + 1
        if self.kind == "swap":
            return sum(len(offs) * 2 for _, offs, _ in self.meta.get("pairs", []))
        if self.kind == "opswap":
            return len(self.meta.get("ops", []))
        return self.end - self.start


# ---------------------------------------------------------------------------
# scanner - decides what is safe to break
# ---------------------------------------------------------------------------

class Scanner:
    def __init__(self, swf, targets, wild=False, opts=None):
        self.swf = swf
        self.body = swf.body
        self.targets = targets
        self.wild = wild
        # opts carries the optional sub-modes:
        #   asset_swap    : bool  - swap same-type character refs in placements
        #   logic_mode    : "bytes" (default) | "opswap"
        # unknown keys are ignored, so older callers keep working
        self.opts = opts or {}
        self.wild = wild
        self.regions = []
        self.tag_counts = {}
        self.stream_format = 2          # assume MP3 streaming audio
        self.skipped = []
        # asset swap needs a second pass once every character is catalogued
        self.characters = {}            # char_id -> category
        self.placements = []            # (byte offset of CharacterId, char_id)
        self.font_glyph_counts = {}     # font_id -> glyph count

    def want(self, target):
        return target in self.targets

    def scan(self):
        start = header_body_start(self.body)
        self._walk(start, len(self.body))
        if self.want("graphics") and self.opts.get("asset_swap"):
            self._build_swap_regions()
        return self.regions

    def _walk(self, start, end):
        for code, b0, b1 in iter_tags(self.body, start, end):
            self.tag_counts[code] = self.tag_counts.get(code, 0) + 1
            self._catalogue(code, b0, b1)
            if code == 39:                       # DefineSprite - recurse
                # body: SpriteID u16, FrameCount u16, then nested tags
                if b1 - b0 >= 4:
                    self.characters[struct.unpack_from("<H", self.body, b0)[0]] \
                        = "sprite"
                    self._walk(b0 + 4, b1)
                continue
            if code in UNTOUCHABLE:
                continue
            try:
                self._tag(code, b0, b1)
            except Exception as exc:             # never let one tag kill a run
                self.skipped.append((code, str(exc)))

    def _catalogue(self, code, b0, b1):
        """Record each defined character's id and category for asset swapping."""
        if b1 - b0 < 2:
            return
        cid = struct.unpack_from("<H", self.body, b0)[0]
        if code in SHAPE_TAGS:
            self.characters[cid] = "shape"
        elif code in (6, 20, 21, 35, 36, 90):      # bitmaps
            self.characters[cid] = "bitmap"
        elif code in TEXT_TAGS or code in EDIT_TEXT_TAGS:
            self.characters[cid] = "text"
        elif code in MORPH_TAGS:
            self.characters[cid] = "morph"
        elif code in BUTTON_TAGS:
            self.characters[cid] = "button"
        elif code in VIDEO_TAGS and code == 60:
            self.characters[cid] = "video"
        if code in FONT_TAGS:
            self.font_glyph_counts[cid] = self._font_glyph_count(code, b0, b1)

    def _font_glyph_count(self, code, b0, b1):
        """Best-effort glyph count so text glyph indices stay in range."""
        try:
            if code == 10:                       # DefineFont: offset table only
                if b0 + 4 > b1:
                    return None
                first = struct.unpack_from("<H", self.body, b0 + 2)[0]
                return first // 2 if first else None
            if code in (48, 75):                 # DefineFont2 / 3
                p = b0 + 2
                flags = self.body[p]; p += 1
                p += 1                           # language
                name_len = self.body[p]; p += 1
                p += name_len
                if p + 2 > b1:
                    return None
                return struct.unpack_from("<H", self.body, p)[0]
        except Exception:
            return None
        return None

    # -- per-tag dispatch ---------------------------------------------------

    def _tag(self, code, b0, b1):
        if code in SOUND_STREAM_HEAD:
            if b1 - b0 >= 2:
                self.stream_format = self.body[b0 + 1] >> 4
            return

        # placements are recorded whenever graphics is active, so asset swap
        # has something to work with; the matrix/cxform regions come too
        if self.want("graphics") and code in PLACE_TAGS:
            self._place(code, b0, b1)

        if self.want("text"):
            if code in TEXT_TAGS:
                self._text_glyphs(code, b0, b1)
            if code in EDIT_TEXT_TAGS:
                self._edit_text(code, b0, b1)

        if self.want("graphics"):
            if code in SHAPE_TAGS:
                return self._shape(code, b0, b1)
            if code in MORPH_TAGS or code in FONT_TAGS \
                    or code in BUTTON_TAGS or code in VIDEO_TAGS:
                return self._generic_character(b0, b1)
            # text tag glyph shapes are fair game as graphics bytes
            if code in TEXT_TAGS:
                return self._generic_character(b0, b1)
            if code in JPEG_TAGS:
                return self._jpeg(code, b0, b1)
            if code in LOSSLESS_TAGS:
                return self._lossless(code, b0, b1)
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
            opswap = self.opts.get("logic_mode") == "opswap"
            if code in ACTION_TAGS:
                return self._do_action(code, b0, b1, opswap=opswap)
            if code in ABC_TAGS:
                return self._do_abc(code, b0, b1, opswap=opswap)

    def _add(self, region):
        if region.size > 0 or region.spans or region.meta:
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

    # -- text ---------------------------------------------------------------

    def _text_glyphs(self, code, b0, b1):
        """Find the glyph-index bit spans in a DefineText / DefineText2 tag.

        Each text record stores GlyphBits-wide indices into its font's glyph
        table. Remapping those indices (within range) rearranges which letters
        draw, so "SCORE" becomes a jumble while the tag stays structurally
        valid. We record the font in use so the corruptor can keep each index
        inside the glyph count it saw at define time.
        """
        bits = Bits(self.body, b0 + 2)          # skip CharacterId
        skip_rect(bits)                         # text bounds
        bits.align()
        # MATRIX
        parse_matrix(bits)
        glyph_bits = bits.read(8)
        advance_bits = bits.read(8)
        if glyph_bits == 0 or glyph_bits > 32 or advance_bits > 32:
            return
        cur_font_glyphs = None
        entries = []                            # (bit position, glyph_bits)
        limit = b1 * 8                          # never read past the tag
        ok = True
        while bits.byte_pos < b1:
            flags = self.body[bits.byte_pos]
            if flags == 0:
                break
            if flags & 0x80:                    # text record header (TEXTRECORD)
                bits.read(8)
                has_font = bool(flags & 0x08)
                has_color = bool(flags & 0x04)
                has_yoff = bool(flags & 0x02)
                has_xoff = bool(flags & 0x01)
                if has_font:
                    fid = bits.read(16)
                    cur_font_glyphs = self.font_glyph_counts.get(fid)
                if has_color:
                    bits.read(32 if code == 33 else 24)
                if has_xoff:
                    bits.read(16)
                if has_yoff:
                    bits.read(16)
                if has_font:
                    bits.read(16)               # text height
                if bits.pos > limit:            # header ran off the end
                    ok = False
                    break
            else:                               # glyph record (GLYPHENTRY run)
                count = bits.read(8)
                need = count * (glyph_bits + advance_bits)
                if bits.pos + need > limit:     # run would spill past the tag
                    ok = False
                    break
                for _ in range(count):
                    gpos = bits.pos
                    bits.read(glyph_bits)
                    bits.read(advance_bits)
                    entries.append((gpos, glyph_bits, cur_font_glyphs))
                # each TEXTRECORD starts byte-aligned, so realign after a run
                bits.align()
        # only keep spans we are certain stay inside the tag; if the parse
        # desynced, corrupt nothing here rather than risk a neighbouring tag
        if ok and entries:
            good = [(p, n, c) for (p, n, c) in entries if p + n <= limit]
            if len(good) == len(entries):
                self._add(Region("glyph", "text", b0, b1,
                                 spans=[(p, n) for p, n, _ in good],
                                 meta={"caps": [c for _, _, c in good]}))

    def _edit_text(self, code, b0, b1):
        """Record the InitialText string of a DefineEditText tag, if present."""
        p = b0 + 2                              # skip CharacterId
        b = Bits(self.body, p)
        skip_rect(b)
        b.align()
        p = b.byte_pos
        if p + 2 > b1:
            return
        flag1 = self.body[p]
        flag2 = self.body[p + 1]
        p += 2
        has_text = bool(flag1 & 0x80)
        has_font = bool(flag1 & 0x01)
        has_font_class = bool(flag2 & 0x80)
        has_color = bool(flag1 & 0x04)
        has_maxlen = bool(flag1 & 0x02)
        has_layout = bool(flag2 & 0x20)
        if has_font:
            p += 4                              # FontID + FontHeight
        if has_font_class:
            end = self.body.find(b"\x00", p, b1)
            if end < 0:
                return
            p = end + 1
        if has_color:
            p += 4                              # RGBA
        if has_maxlen:
            p += 2
        if has_layout:
            p += 9                              # align + 4 * u16
        # VariableName (always present): skip it, it is a symbol
        end = self.body.find(b"\x00", p, b1)
        if end < 0:
            return
        p = end + 1
        if has_text and p < b1:
            end = self.body.find(b"\x00", p, b1)
            if end < 0:
                end = b1
            if end > p:
                self._add(Region("ascii", "text", p, end))

    def _build_swap_regions(self):
        """Pair up placements that reference same-category characters and
        emit one swap region that exchanges their CharacterIds in place."""
        buckets = {}
        for off, cid in self.placements:
            cat = self.characters.get(cid)
            if cat is None:
                continue
            buckets.setdefault(cat, []).append((off, cid))
        pairs = []
        for cat, items in buckets.items():
            # distinct target ids within the category
            ids = sorted({cid for _, cid in items})
            if len(ids) < 2:
                continue
            offs = [off for off, _ in items]
            pairs.append((cat, offs, ids))
        if pairs:
            self._add(Region("swap", "graphics", 0, 0,
                             meta={"pairs": pairs}))

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
            char_off = bits.byte_pos            # byte offset of CharacterId
            char_id = struct.unpack_from("<H", self.body, char_off)[0]
            self.placements.append((char_off, char_id))
            bits.read(16)                       # CharacterId - not byte-corrupted
        # SWF field order after CharacterId is Matrix, then ColorTransform, then
        # Ratio, then Name. Matrix and cxform are all we touch and they come
        # first, so there is no Name to skip here - doing so would eat matrix
        # bytes and desync every following field.
        spans = []
        if has_matrix:
            spans += parse_matrix(bits)
        if has_cxform:
            spans += parse_cxform(bits, with_alpha=(code == 70))
        # keep only spans that stay inside this tag; a misparse never
        # corrupts a neighbour
        limit = b1 * 8
        spans = [(s, n) for (s, n) in spans if s + n <= limit]
        if spans:
            self._add(Region("bits", "graphics", b0, b1, spans=spans))
        _ = (has_clip_actions, has_clip_depth, has_ratio, has_name)

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

    def _do_action(self, code, b0, b1, opswap=False):
        pos = b0
        if code == 59:                          # DoInitAction: SpriteID first
            pos += 2
        spans = []
        ops = []
        while pos < b1:
            op = self.body[pos]
            if op < 0x80:                       # single-byte action, no operand
                if opswap and op in AS2_SWAP:
                    ops.append((pos, AS2_SWAP[op]))
                pos += 1
                continue
            if pos + 3 > b1:
                break
            length = struct.unpack_from("<H", self.body, pos + 1)[0]
            data0 = pos + 3
            data1 = min(data0 + length, b1)
            if not opswap:
                if op == 0x96:                  # ActionPush
                    spans += self._push_spans(data0, data1)
                elif op == 0x88:                # ActionConstantPool
                    spans += self._pool_spans(data0, data1)
            pos = data1
        if opswap:
            if ops:
                self._add(Region("opswap", "logic", b0, b1, meta={"ops": ops}))
            return
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

    def _do_abc(self, code, b0, b1, opswap=False):
        # Operator swapping in AVM2 needs a full method-body bytecode walker to
        # avoid mistaking operand bytes for opcodes; that is out of scope, so
        # for ABC we fall back to the safe constant-pool corruption even when
        # opswap is requested. AS2 (DoAction) gets real operator swaps.
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
    if target == "text":
        # text is sparse too - a handful of glyphs per field - and people
        # expect a mid dial to clearly garble it
        return min(p * 12.0, 0.95)
    return min(p, 0.9)


class Corruptor:
    def __init__(self, body, rng, strengths, wild=False, opts=None):
        self.body = body
        self.rng = rng
        self.strengths = strengths
        self.wild = wild
        self.opts = opts or {}
        self.stats = {}

    def hit(self, target, n=1):
        self.stats[target] = self.stats.get(target, 0) + n

    def run(self, regions):
        for r in regions:
            s = self.strengths.get(r.target, 0)
            if r.kind in ("swap", "opswap"):
                # discrete toggles: a linear rate so a mid dial swaps ~half.
                # These are opt-in sub-modes, so even a low or zero dial should
                # visibly do something - floor the rate at 0.5.
                p = max(0.5, min(1.0, s / 100.0))
            else:
                p = strength_to_p(s, r.target)
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
        tag_end_bit = r.end * 8                   # never flip outside the tag
        for bit_start, nbits in r.spans:
            if bit_start + nbits > tag_end_bit:   # stray span, skip entirely
                continue
            first = 0 if self.wild else 1        # keep the sign bit sane
            for k in range(first, nbits):
                if rng.random() < bit_p:
                    idx = bit_start + k
                    byte_i = idx >> 3
                    if byte_i >= len(self.body):
                        continue
                    self.body[byte_i] ^= 1 << (7 - (idx & 7))
                    self.hit(r.target)

    def _do_glyph(self, r, p):
        """Remap glyph indices, each kept inside its font's glyph count."""
        rng = self.rng
        caps = r.meta.get("caps", [])
        tag_end_bit = r.end * 8                  # never write past the tag
        for (bit_start, nbits), cap in zip(r.spans, caps):
            if bit_start + nbits > tag_end_bit:  # scan recorded a stray span
                continue
            if rng.random() >= p:
                continue
            hi = (cap - 1) if cap and cap > 1 else ((1 << nbits) - 1)
            if hi <= 0:
                continue
            new = rng.randint(0, hi)
            self._write_bits(bit_start, nbits, new)
            self.hit(r.target)

    def _write_bits(self, bit_start, nbits, value):
        body = self.body
        for k in range(nbits):
            bit = (value >> (nbits - 1 - k)) & 1
            idx = bit_start + k
            byte_i = idx >> 3
            if byte_i >= len(body):
                return
            mask = 1 << (7 - (idx & 7))
            if bit:
                body[byte_i] |= mask
            else:
                body[byte_i] &= ~mask & 0xFF

    def _do_swap(self, r, p):
        """Exchange CharacterIds among same-category placements."""
        rng = self.rng
        for cat, offs, ids in r.meta.get("pairs", []):
            if len(ids) < 2:
                continue
            swapped = 0
            for off in offs:
                if rng.random() >= p:
                    continue
                swapped += self._swap_one(off, ids)
            if swapped == 0 and offs:
                # the dice all missed; guarantee at least one visible swap so
                # turning the feature on is never a no-op
                self._swap_one(rng.choice(offs), ids)

    def _swap_one(self, off, ids):
        cur = struct.unpack_from("<H", self.body, off)[0]
        choices = [i for i in ids if i != cur]
        if not choices:
            return 0
        struct.pack_into("<H", self.body, off, self.rng.choice(choices))
        self.hit("graphics")
        return 1

    def _do_opswap(self, r, p):
        """Swap an operator opcode for its same-length, same-arity sibling."""
        rng = self.rng
        for off, alts in r.meta.get("ops", []):
            if rng.random() >= p:
                continue
            self.body[off] = rng.choice(alts)
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
    for target in ALL_TARGETS:
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
    out = {t: 0.0 for t in ALL_TARGETS}
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


def verify_structure(orig_body, new_body):
    """Raise if the two bodies do not have identical tag geometry.

    Length-preserving corruption must never change where a tag starts or how
    long it is; this catches any scan bug that would have shifted the stream.
    """
    sa = header_body_start(orig_body)
    sb = header_body_start(new_body)
    if orig_body[:sa] != new_body[:sb]:
        raise ValueError("SWF header changed")

    def walk(body, start, end):
        n = 0
        for code, b0, b1 in iter_tags(body, start, end):
            n += 1
            if code == 39 and b1 - b0 >= 4:
                n += walk(body, b0 + 4, b1)
        return n

    if walk(orig_body, sa, len(orig_body)) != walk(new_body, sb, len(new_body)):
        raise ValueError("tag count changed")
    for (ca, a0, a1), (cb, c0, c1) in zip(
            iter_tags(orig_body, sa, len(orig_body)),
            iter_tags(new_body, sb, len(new_body))):
        if (ca, a0, a1) != (cb, c0, c1):
            raise ValueError("tag geometry changed at %d" % a0)


def corrupt_once(path, out_path, targets, strengths, seed, wild, compress,
                 quiet=False, opts=None):
    swf = Swf.load(path)
    original = bytes(swf.body)                    # for the structure check
    scanner = Scanner(swf, targets, wild=wild, opts=opts)
    regions = scanner.scan()
    rng = random.Random(seed)
    corruptor = Corruptor(swf.body, rng, strengths, wild=wild, opts=opts)
    stats = corruptor.run(regions)
    # refuse to write a file that would not load - the whole point of the tool
    verify_structure(original, swf.body)
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
  flashbang game.swf -o out.swf -t graphics,text -s graphics=60,text=40
  flashbang game.swf -o out.swf -t graphics --asset-swap
  flashbang game.swf -o out.swf -t logic --opswap -s 100
""")
    ap.add_argument("input", help="input .swf")
    ap.add_argument("-o", "--output", help="output .swf")
    ap.add_argument("-t", "--target", default="all",
                    help="graphics, sound, logic, text, all (comma separated)")
    ap.add_argument("-s", "--strength", default="25",
                    help="0-100, or per-target like graphics=60,text=40")
    ap.add_argument("--seed", type=int, default=None,
                    help="RNG seed - same seed gives the same corruption")
    ap.add_argument("--wild", action="store_true",
                    help="unlock riskier regions (bounds, sign bits, bg colour)")
    ap.add_argument("--asset-swap", action="store_true",
                    help="graphics: swap same-type character refs in placements")
    ap.add_argument("--opswap", action="store_true",
                    help="logic: swap operators (+/-, </>) instead of bytes (AS2)")
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
        targets = set(ALL_TARGETS)
    else:
        targets = set(raw_targets)
    bad = targets - set(ALL_TARGETS)
    if bad:
        ap.error("unknown target(s): %s" % ", ".join(sorted(bad)))

    opts = {}
    if args.asset_swap:
        opts["asset_swap"] = True
    if args.opswap:
        opts["logic_mode"] = "opswap"

    try:
        swf = Swf.load(args.input)
    except Exception as exc:
        ap.error(str(exc))

    if args.report:
        scanner = Scanner(swf, targets, wild=args.wild, opts=opts)
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
                 compress, args.quiet, opts=opts)

    if not args.quiet:
        print()
        print("  done. reuse --seed %d to reproduce this exact run." % seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
