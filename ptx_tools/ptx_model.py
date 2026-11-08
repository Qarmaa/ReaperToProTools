#!/usr/bin/env python3
"""
ptx_model.py — модель .ptx с пересчётом смещений при сохранении.

Нужна для правок, меняющих размеры блоков (добавление клипов, переименование):
все абсолютные смещения лежат в финальной таблице 0x0002 (15-байтные записи
`00 00 00 01 04 00 01 00 | target u32 | 00 00 00`) и в указателе 0x0001 в начале
файла. При сохранении:
  1. сериализуем дерево и запоминаем старый -> новый offset каждого блока;
  2. в 0x0002 переписываем target'ы стандартных записей и любые другие
     4-байтные слова метаданных, совпадающие со старым offset блока;
  3. патчим указатель 0x0001 на новый 0x0002.

Формат контейнера описан в открытой спецификации pt_api (MIT) и подтверждён на
сессиях Pro Tools 26.4: байты, которые мы не понимаем, сохраняются как есть.

    python3 ptx_model.py roundtrip file.ptx     # load -> save == исходник?
"""
import struct
import sys

sys.path.insert(0, '.')
import ptx

# Типы с плоским (фиксированным) содержимым: внутри не ищем вложенные блоки.
FLAT = {0x0002, 0x1028, 0x104f, 0x204d, 0x260a, 0x262f, 0x2637}
STD_REC = bytes.fromhex('0000000104000100')


def _is_rec(tbl, i):
    """Стандартная запись таблицы в позиции i. Счётчик серии — u16 LE: младший байт — последний байт перед первой записью
    серии, старший заменяет первый байт (00) первой записи; при счётчике >= 256 первая запись начинается не с 00
    (подтверждено на сессиях Pro Tools с 255..512 дорожками)."""
    if tbl[i + 12:i + 15] != b'\0\0\0' or tbl[i + 1:i + 8] != STD_REC[1:]:
        return False
    if tbl[i] == 0:
        return True
    return i >= 6 and tbl[i - 6:i - 1] == b'\x01\x00\x00\x00\x00'


class Block:
    """block_type + content (bytes | Block)*; ctype входит в первые 2 байта содержимого."""

    def __init__(self, btype, items, orig=None):
        self.btype = btype
        self.items = items          # список bytes / Block
        self.orig = orig            # исходное смещение (или None для нового блока)

    @property
    def ctype(self):
        items = self.items
        if items:
            first = items[0]
            if not isinstance(first, Block) and len(first) >= 2:         # ctype — первые 2 байта содержимого
                return first[0] | (first[1] << 8)
        return None

    def size(self):
        return sum(len(i) if isinstance(i, (bytes, bytearray)) else i.total() for i in self.items)

    def total(self):
        return 7 + self.size()


def parse(buf, pos, limit):
    """Разбор блока в pos; None если там не блок."""
    if pos + 7 > limit or buf[pos] != 0x5A:
        return None
    btype, size = struct.unpack_from('<HI', buf, pos + 1)
    if btype & 0xFF00 or pos + 7 + size > limit or size <= 1:         # у настоящего блока в содержимом есть ctype (>= 2 байт); size 0/1 — случайные байты `5a ..`
        return None
    body_start, end = pos + 7, pos + 7 + size
    blk = Block(btype, [], pos)
    ctype = struct.unpack_from('<H', buf, body_start)[0]
    if ctype in FLAT and pos != 0x14:
        blk.items = [bytes(buf[body_start:end])]
        return blk
    items = []
    i = body_start + 2
    raw_from = body_start
    find = buf.find
    while i < end:
        i = find(b'\x5a', i, end)                       # блок может начаться только с 0x5A: остальные байты пропускаем без вызова parse
        if i < 0:
            break
        child = parse(buf, i, end)
        if child:
            if raw_from < i:
                items.append(bytes(buf[raw_from:i]))
            items.append(child)
            i += child.total()
            raw_from = i
        else:
            i += 1
    if raw_from < end:
        items.append(bytes(buf[raw_from:end]))
    blk.items = items
    return blk



def _is_list_header(tbl, i, known):
    """Слово `4N 00 N 00` (заголовок списка из N указателей: длина в байтах u16, число элементов u16), за которым идут N
    указателей. Такое слово — данные: оно может случайно совпасть со смещением блока (например 08 00 02 00 = 0x20008) и
    при пересчёте смещений превращалось бы в «указатель»; это зависело от суммарной длины имён дорожек."""
    if i + 4 > len(tbl):
        return False
    lo, hi = struct.unpack_from('<HH', tbl, i)
    if not (1 <= hi <= 256 and lo == 4 * hi) or i + 4 + 4 * hi > len(tbl):
        return False
    return all(struct.unpack_from('<I', tbl, i + 4 + 4 * k)[0] in known for k in range(hi))


def _is_unit_number(tbl, i):
    """Заголовок юнита таблицы `01 K 00 00 | 00 xx 25|26` (K — номер юнита, за ним ctype 0x251a/0x251c/0x261c...): слово `01 K 00 00` может случайно
    совпасть со смещением блока (например K = 5 и блок по смещению 0x501), но это число, а не указатель — его нельзя пересчитывать."""
    return (i + 7 <= len(tbl) and tbl[i] == 1 and tbl[i + 2:i + 5] == b'\x00\x00\x00' and tbl[i + 5] in (0x1a, 0x1b, 0x1c, 0x1d, 0x1e)
            and tbl[i + 6] in (0x25, 0x26))


class PTX:
    def __init__(self, raw):
        self.header = bytes(raw[:0x14])
        buf = ptx.crypt(raw)
        if buf[0x14] != 0x5A or struct.unpack_from('<H', buf, 0x15)[0] != 1:
            raise ValueError('block 0x0001 not found at 0x14')
        # 0x0001: 7 байт заголовка + 4 байта указателя; содержимое не содержит ctype.
        self.pointer = struct.unpack_from('<I', buf, 0x1B)[0]
        self.table_pos = self.pointer
        # корневые блоки между 0x1f и таблицей; всё, что не блок — сохраняем как bytes
        self.roots = []
        pos = 0x1F
        raw_from = pos
        while pos < self.table_pos:
            b = parse(buf, pos, self.table_pos)
            if b:
                if raw_from < pos:
                    self.roots.append(bytes(buf[raw_from:pos]))
                self.roots.append(b)
                pos += b.total()
                raw_from = pos
            else:
                pos += 1
        if raw_from < self.table_pos:
            self.roots.append(bytes(buf[raw_from:self.table_pos]))
        t = parse(buf, self.table_pos, len(buf))
        if not t or self.table_pos + t.total() != len(buf):
            raise ValueError('final 0x0002 table must end the file')
        self.table = bytearray(b''.join(t.items))      # включает ctype 0x0002
        self.table_orig = self.table_pos
        self.extra = []        # (after_block, new_block): новая запись таблицы сразу за записью after_block
        self.rec_hi = {}       # id(блок первой записи серии) -> старший байт счётчика серии (если >= 256)
        self.tokens = None     # символьное представление таблицы (см. tokenize); None — таблица правится как байты

    # ---- обход -------------------------------------------------------
    def blocks(self, items=None):
        for i in (self.roots if items is None else items):
            if isinstance(i, Block):
                yield i
                yield from self.blocks(i.items)

    # ---- символьная таблица -------------------------------------------
    def tokenize(self):
        """Таблица 0x0002 как список токенов: ('b', bytes) | ('rec', Block) | ('ptr', Block).

        Для правок, добавляющих блоки: токены можно вставлять/копировать/переставлять, а смещения расставляются при
        сохранении. Указателем считается слово по тем же правилам, что и при пересчёте в save()."""
        orig = {}
        for b in self.blocks():
            if b.orig is not None:
                orig[b.orig] = b
        ctypes = {b.ctype for b in self.blocks()} | {2}
        tbl = bytes(self.table)
        covered = set()
        i = 2
        while i <= len(tbl) - 15:
            if _is_rec(tbl, i):
                covered.update(range(i, i + 15))
                i += 15
            else:
                i += 1
        toks, buf = [], bytearray()
        i, last = 0, -10
        def flush():
            nonlocal buf
            if buf:
                toks.append(('b', bytes(buf)))
                buf = bytearray()
        while i < len(tbl):
            if i in covered and _is_rec(tbl, i):
                v = struct.unpack_from('<I', tbl, i + 8)[0]
                if v not in orig:
                    raise ValueError(f'table record target {v:#x} is not a block')
                flush()
                if tbl[i]:
                    self.rec_hi[id(orig[v])] = tbl[i]
                toks.append(('b', STD_REC))
                toks.append(('rec', orig[v]))
                toks.append(('b', b'\0\0\0'))
                i += 15
                continue
            if i >= 2 and i + 4 <= len(tbl) and not any(k in covered for k in range(i, i + 4)) and i >= last + 4:
                v = struct.unpack_from('<I', tbl, i)[0]
                if v in orig and v != self.table_orig and not _is_list_header(tbl, i, orig) and not _is_unit_number(tbl, i):
                    prev = struct.unpack_from('<H', tbl, i - 2)[0] if i >= 4 else 0
                    nxt = struct.unpack_from('<I', tbl, i + 4)[0] if i + 8 <= len(tbl) else 0
                    one = prev == 1 and i >= 6 and struct.unpack_from('<H', tbl, i - 4)[0] == 4      # список из одного указателя: `04 00 01 00 <ptr>`
                    if prev in ctypes or i == last + 4 or nxt in orig or one:
                        flush()
                        toks.append(('ptr', orig[v]))
                        i += 4
                        last = i - 4
                        continue
            buf.append(tbl[i])
            i += 1
        flush()
        # ('b', 8 байт STD_REC) + ('rec') + ('b', 000000) — склеим в один токен ('rec', block) для удобства
        out, k = [], 0
        while k < len(toks):
            t = toks[k]
            if t[0] == 'b' and k + 2 < len(toks) and toks[k + 1][0] == 'rec':
                head = t[1]
                if head.endswith(STD_REC):
                    if len(head) > 8:
                        out.append(('b', head[:-8]))
                    out.append(('rec', toks[k + 1][1]))
                    tail = toks[k + 2][1]
                    assert tail[:3] == b'\0\0\0'
                    if len(tail) > 3:
                        toks[k + 2] = ('b', tail[3:])
                        k += 2
                    else:
                        k += 3
                    continue
            out.append(t)
            k += 1
        self.tokens = out
        return out

    def _serialize_tokens(self, newoff, table_new):
        out = bytearray()
        for kind, v in self.tokens:
            if kind == 'b':
                out += v
            elif kind == 'rec':
                out += bytes([self.rec_hi.get(id(v), 0)]) + STD_REC[1:] + struct.pack('<I', newoff[id(v)]) + b'\0\0\0'
            else:
                out += struct.pack('<I', newoff[id(v)])
        return out

    # ---- сохранение --------------------------------------------------
    def save(self):
        out = bytearray(self.header)
        out += b'\x5a\x01\x00\x04\x00\x00\x00' + b'\0\0\0\0'        # 0x0001, указатель позже

        offmap = {}
        newoff = {}                                  # id(block) -> новое смещение (и для новых блоков)
        sizes = {}                                   # размеры блоков считаются один раз (иначе рекурсия на каждом уровне)

        def sz(b):
            v = sizes.get(id(b))
            if v is None:
                v = sizes[id(b)] = sum(len(x) if isinstance(x, (bytes, bytearray)) else 7 + sz(x) for x in b.items)
            return v

        def emit(items):
            for i in items:
                if isinstance(i, (bytes, bytearray)):
                    out.extend(i)
                else:
                    newoff[id(i)] = len(out)
                    if i.orig is not None:
                        offmap[i.orig] = len(out)
                    out.extend(b'\x5a' + struct.pack('<HI', i.btype, sz(i)))
                    emit(i.items)

        emit(self.roots)
        new_table = len(out)
        offmap[self.table_orig] = new_table

        # ---- таблица 0x0002 -----------------------------------------
        if self.tokens is not None:
            tbl = self._serialize_tokens(newoff, new_table)
            out += b'\x5a' + struct.pack('<HI', 0x02, len(tbl)) + bytes(tbl)
            struct.pack_into('<I', out, 0x14 + 7, new_table)
            return ptx.crypt(bytes(out), self.header)
        tbl = bytearray(self.table)
        rec = []
        i = 2                                  # пропускаем ctype
        while i <= len(tbl) - 15:
            if _is_rec(tbl, i):
                rec.append(i)
                i += 15
            else:
                i += 1
        covered = set()
        for r in rec:
            old = struct.unpack_from('<I', tbl, r + 8)[0]
            if old not in offmap:
                raise ValueError(f'table target {old:#x} is not a known block')
            struct.pack_into('<I', tbl, r + 8, offmap[old])
            covered.update(range(r, r + 15))
        # прочие абсолютные смещения в метаданных между сериями
        # Слово считается указателем только если перед ним стоит u16 ctype блока из этого файла (или 0x0002 —
        # начало пары), либо оно идёт вплотную за другим указателем, либо перед ним (голова списка со счётчиком).
        # Иначе случайное совпадение числа (флаги `00 01 01 00` = 65792, счётчик 0x600, число дорожек 31 ...)
        # с чьим-то смещением портит таблицу — и только при некоторых длинах имён.
        ctypes = {b.ctype for b in self.blocks()} | {2}
        last_ptr = -10
        for p in range(2, len(tbl) - 3):
            if any(q in covered for q in range(p, p + 4)) or p < last_ptr + 4:
                continue
            v = struct.unpack_from('<I', tbl, p)[0]
            if v in offmap and v != self.table_orig and not _is_list_header(tbl, p, offmap) and not _is_unit_number(tbl, p):
                prev = struct.unpack_from('<H', tbl, p - 2)[0] if p >= 4 else 0
                nxt = struct.unpack_from('<I', tbl, p + 4)[0] if p + 8 <= len(tbl) else 0
                if prev in ctypes or p == last_ptr + 4 or nxt in offmap:
                    struct.pack_into('<I', tbl, p, offmap[v])
                    last_ptr = p

        # дополнительные записи: вставляем сразу за записью родителя и увеличиваем BE-счётчик серии
        for after_block, new_block in self.extra:
            tgt = struct.pack('<I', newoff[id(after_block)])
            pos = None
            for r in rec:
                if bytes(tbl[r + 8:r + 12]) == tgt:
                    pos = r
                    break
            if pos is None:
                raise ValueError('record of the parent block not found in the table')
            # начало серии: идём назад по соседним записям
            start = pos
            while start - 15 >= 2 and _is_rec(tbl, start - 15):
                start -= 15
            count = tbl[start - 1] | (tbl[start] << 8)
            n_series = 0
            q = start
            while q + 15 <= len(tbl) and _is_rec(tbl, q):
                n_series += 1
                q += 15
            if count != n_series:
                raise ValueError(f'series counter {count} != {n_series} records')
            newrec = STD_REC + struct.pack('<I', newoff[id(new_block)]) + b'\0\0\0'
            tbl[pos + 15:pos + 15] = newrec
            tbl[start - 1] = (count + 1) & 255
            tbl[start] = (count + 1) >> 8
            rec = [r + 15 if r > pos else r for r in rec] + [pos + 15]
            rec.sort()

        out += b'\x5a' + struct.pack('<HI', 0x02, len(tbl)) + bytes(tbl)
        struct.pack_into('<I', out, 0x14 + 7, new_table)
        return ptx.crypt(bytes(out), self.header)


def roundtrip(path):
    raw = open(path, 'rb').read()
    p = PTX(raw)
    out = p.save()
    ok = out == raw
    print(f'{path.split("/")[-1]}: {len(raw)} bytes -> {len(out)} bytes, roundtrip {"OK" if ok else "FAIL"}')
    if not ok:
        for i, (a, b) in enumerate(zip(raw, out)):
            if a != b:
                print('  first diff at', hex(i))
                break
    return ok


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == 'roundtrip':
        sys.exit(0 if all(roundtrip(p) for p in sys.argv[2:]) else 1)
    print(__doc__)
