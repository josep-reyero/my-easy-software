"""A small pure-Python BLAKE3 (unkeyed hash, 32-byte output), standard library only.

Used only to name files the way Cloudflare Pages' own upload tool (wrangler) does: the name of
each uploaded file is ``blake3(base64(content) + extension)`` in hex, first 32 characters. It is
content addressing, not a security check. It follows the reference implementation of the BLAKE3
paper and is checked against the published test vectors.

Public API
----------
``blake3(data: bytes) -> bytes``   the 32-byte digest.
``hexdigest(data: bytes) -> str``
"""

from __future__ import annotations

IV = (0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A, 0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19)
MSG_PERMUTATION = (2, 6, 3, 10, 7, 0, 4, 13, 1, 11, 12, 5, 9, 14, 15, 8)
CHUNK_START, CHUNK_END, PARENT, ROOT = 1, 2, 4, 8
CHUNK_LEN = 1024
BLOCK_LEN = 64
MASK = 0xFFFFFFFF


def _rotr(x: int, n: int) -> int:
    return ((x >> n) | (x << (32 - n))) & MASK


def _g(s: list[int], a: int, b: int, c: int, d: int, mx: int, my: int) -> None:
    s[a] = (s[a] + s[b] + mx) & MASK
    s[d] = _rotr(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & MASK
    s[b] = _rotr(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b] + my) & MASK
    s[d] = _rotr(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & MASK
    s[b] = _rotr(s[b] ^ s[c], 7)


def _round(s: list[int], m: list[int]) -> None:
    _g(s, 0, 4, 8, 12, m[0], m[1])
    _g(s, 1, 5, 9, 13, m[2], m[3])
    _g(s, 2, 6, 10, 14, m[4], m[5])
    _g(s, 3, 7, 11, 15, m[6], m[7])
    _g(s, 0, 5, 10, 15, m[8], m[9])
    _g(s, 1, 6, 11, 12, m[10], m[11])
    _g(s, 2, 7, 8, 13, m[12], m[13])
    _g(s, 3, 4, 9, 14, m[14], m[15])


def _compress(cv: list[int], block: list[int], counter: int, block_len: int, flags: int) -> list[int]:
    s = [*cv, *IV[:4], counter & MASK, (counter >> 32) & MASK, block_len, flags]
    m = list(block)
    for i in range(7):
        _round(s, m)
        if i < 6:
            m = [m[p] for p in MSG_PERMUTATION]
    for i in range(8):
        s[i] ^= s[i + 8]
        s[i + 8] ^= cv[i]
    return s


def _words(block: bytes) -> list[int]:
    block = block.ljust(BLOCK_LEN, b"\x00")
    return [int.from_bytes(block[i:i + 4], "little") for i in range(0, BLOCK_LEN, 4)]


class _Output:
    def __init__(self, cv: list[int], block: list[int], counter: int, block_len: int, flags: int) -> None:
        self.cv, self.block, self.counter, self.block_len, self.flags = cv, block, counter, block_len, flags

    def chaining_value(self) -> list[int]:
        return _compress(self.cv, self.block, self.counter, self.block_len, self.flags)[:8]

    def root_bytes(self, length: int = 32) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < length:
            words = _compress(self.cv, self.block, counter, self.block_len, self.flags | ROOT)
            out += b"".join(w.to_bytes(4, "little") for w in words)
            counter += 1
        return bytes(out[:length])


class _ChunkState:
    def __init__(self, key: list[int], counter: int) -> None:
        self.cv = list(key)
        self.counter = counter
        self.buf = b""
        self.blocks = 0

    def length(self) -> int:
        return BLOCK_LEN * self.blocks + len(self.buf)

    def _start(self) -> int:
        return CHUNK_START if self.blocks == 0 else 0

    def update(self, data: bytes) -> None:
        while data:
            if len(self.buf) == BLOCK_LEN:
                self.cv = _compress(self.cv, _words(self.buf), self.counter, BLOCK_LEN, self._start())[:8]
                self.blocks += 1
                self.buf = b""
            take = BLOCK_LEN - len(self.buf)
            self.buf += data[:take]
            data = data[take:]

    def output(self) -> _Output:
        return _Output(self.cv, _words(self.buf), self.counter, len(self.buf), self._start() | CHUNK_END)


def _parent(left: list[int], right: list[int], key: list[int]) -> _Output:
    return _Output(list(key), [*left, *right], 0, BLOCK_LEN, PARENT)


def blake3(data: bytes) -> bytes:
    key = list(IV)
    chunk = _ChunkState(key, 0)
    stack: list[list[int]] = []
    view = memoryview(bytes(data))
    while len(view):
        if chunk.length() == CHUNK_LEN:
            cv = chunk.output().chaining_value()
            total = chunk.counter + 1
            while total & 1 == 0:
                cv = _parent(stack.pop(), cv, key).chaining_value()
                total >>= 1
            stack.append(cv)
            chunk = _ChunkState(key, chunk.counter + 1)
        take = CHUNK_LEN - chunk.length()
        chunk.update(bytes(view[:take]))
        view = view[take:]
    out = chunk.output()
    for left in reversed(stack):
        out = _parent(left, out.chaining_value(), key)
    return out.root_bytes(32)


def hexdigest(data: bytes) -> str:
    return blake3(data).hex()
