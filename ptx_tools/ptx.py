#!/usr/bin/env python3
"""
ptx.py — чтение/запись контейнера Pro Tools .ptx (поколение PT 10-12+, xor_type 5).

Формат (по открытой библиотеке ptformat и собственным наблюдениям):
  * первые 0x14 байт не шифруются; байты 0x12 (xor_type) и 0x13 (xor_value)
    задают ключ;
  * остальное — XOR с 256-байтным ключом, индекс = (смещение >> 12) & 0xff;
    шифр симметричен, поэтому расшифровка и зашифровка — одна операция;
  * внутри — дерево блоков: 0x5A | block_type(2) | size(4) | content_type(2) | данные,
    дочерние блоки лежат внутри данных родителя.

Команды:
  ptx.py tree  file.ptx [--depth N] [--min-size N]   дерево блоков
  ptx.py raw   file.ptx out.bin                      расшифрованный файл
  ptx.py enc   decrypted.bin orig.ptx out.ptx        зашифровать обратно (заголовок из orig)
  ptx.py roundtrip file.ptx                          проверка: unxor -> xor == исходник
"""
import struct
import sys

ZMARK = 0x5A


def xor_key(xor_type: int, xor_value: int):
    if xor_type == 0x01:
        mul, neg = 53, False
    elif xor_type == 0x05:
        mul, neg = 11, True
    else:
        raise ValueError(f"unsupported xor_type {xor_type:#x}")
    delta = 0
    for i in range(256):
        if (i * mul) & 0xFF == xor_value:
            delta = (-i) & 0xFF if neg else i
            break
    return bytes((i * delta) & 0xFF for i in range(256))


_XOR_TABLES = {}


def crypt(data: bytes, header_from: bytes = None) -> bytes:
    """XOR-преобразование (шифр симметричен). Заголовок берётся из header_from или data.

    Ключ меняется раз в 4096 байт, поэтому каждая страница преобразуется одним bytes.translate (в десятки раз быстрее побайтового XOR)."""
    head = (header_from or data)[:0x14]
    key = xor_key(head[0x12], head[0x13])
    n = len(data)
    out = bytearray(n)
    out[:0x14] = head
    pos = 0x14
    while pos < n:
        end = min(n, ((pos >> 12) + 1) << 12)
        k = key[(pos >> 12) & 0xFF]
        tbl = _XOR_TABLES.get(k)
        if tbl is None:
            tbl = _XOR_TABLES[k] = bytes(x ^ k for x in range(256))
        out[pos:end] = bytes(data[pos:end]).translate(tbl)
        pos = end
    return bytes(out)


class Block:
    __slots__ = ("pos", "btype", "size", "ctype", "children", "parent")

    def __init__(self, pos, btype, size, ctype, parent=None):
        self.pos = pos          # смещение ZMARK
        self.btype = btype
        self.size = size
        self.ctype = ctype
        self.children = []
        self.parent = parent

    @property
    def start(self):            # начало содержимого (после ctype)
        return self.pos + 9

    @property
    def end(self):              # конец блока
        return self.pos + 7 + self.size


def parse_block(buf: bytes, pos: int, limit: int, le: bool, parent=None):
    if pos + 9 > len(buf) or buf[pos] != ZMARK:
        return None
    fmt = "<" if le else ">"
    btype, size, ctype = struct.unpack_from(fmt + "HIH", buf, pos + 1)
    if btype & 0xFF00:
        return None
    if pos + 7 + size > limit:
        return None
    b = Block(pos, btype, size, ctype, parent)
    i = 1
    jump = 0
    while i < size and pos + i + jump < limit:
        c = parse_block(buf, pos + i, b.end, le, b)
        if c:
            b.children.append(c)
            jump = c.size + 7
        else:
            jump = 0
        i += jump if jump else 1
    return b


def parse_tree(buf: bytes):
    le = not buf[0x11]
    blocks = []
    pos = 0x1F
    # верхний уровень: блоки идут подряд
    while pos < len(buf):
        b = parse_block(buf, pos, len(buf), le)
        if b:
            blocks.append(b)
            pos = b.end
        else:
            pos += 1
    return blocks, le


def own_payload(buf: bytes, b: Block) -> bytes:
    """Байты блока без вложенных блоков (то, что лежит между детьми)."""
    out = bytearray()
    cur = b.start
    for c in b.children:
        out += buf[cur:c.pos]
        cur = c.end
    out += buf[cur:b.end]
    return bytes(out)


def dump(buf, blocks, depth, min_size, level=0, show=24):
    for b in blocks:
        if level > depth:
            continue
        if b.size < min_size:
            continue
        pay = own_payload(buf, b)
        hx = pay[:show].hex(" ")
        more = "…" if len(pay) > show else ""
        print(f"{'  ' * level}{b.ctype:#06x} t={b.btype} size={b.size} @{b.pos:#x} "
              f"own={len(pay)} [{hx}{more}]")
        dump(buf, b.children, depth, min_size, level + 1, show)


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 1
    cmd, path = argv[1], argv[2]
    raw = open(path, "rb").read()

    if cmd == "tree":
        depth = int(argv[argv.index("--depth") + 1]) if "--depth" in argv else 99
        min_size = int(argv[argv.index("--min-size") + 1]) if "--min-size" in argv else 0
        buf = crypt(raw)
        blocks, le = parse_tree(buf)
        print(f"# {path}: {len(raw)} bytes, xor_type={raw[0x12]:#x} "
              f"value={raw[0x13]:#x} endian={'LE' if le else 'BE'}, {len(blocks)} top blocks")
        dump(buf, blocks, depth, min_size)
    elif cmd == "raw":
        open(argv[3], "wb").write(crypt(raw))
    elif cmd == "enc":
        orig = open(argv[3], "rb").read()
        open(argv[4], "wb").write(crypt(raw, orig))
    elif cmd == "roundtrip":
        ok = crypt(crypt(raw), raw) == raw
        print("roundtrip", "OK" if ok else "FAIL")
        return 0 if ok else 1
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
