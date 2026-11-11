"""Швидкий ZIP для проєкту з сіткою: стиснення і розпаковка в кілька потоків.

Чому не просто zipfile: він стискає кожен файл в ОДИН потік. Сітка на
2.4 млн комірок — це 290 МБ (уже бінарна), і запис ішов 16 с, а
автозбереження перед прогоном (сітку переносимо зі старого файлу) — 17 с.

Як тут: файл ріжеться на шматки по 1 МіБ, кожен шматок стискається окремо
з Z_FULL_FLUSH (без словника попереднього шматка), і шматки склеюються в
ОДИН звичайний потік DEFLATE — так робить pigz. Архів лишається звичайним
ZIP: його читає zipfile, 7-Zip, Провідник Windows. zlib відпускає GIL, тож
потоки справді йдуть паралельно.

Рівень 1, а не 6: на бінарній сітці різниця в розмірі 0.3 % (136.4 проти
136.0 МБ), а час утричі менший (виміряно 2026-11-10).

Розпаковка: розміри стиснених шматків лежать в окремому записі-індексі
(див. fs_library.PROJECT_MESH_INDEX). За ними кожен шматок розпаковується
окремо, теж у кілька потоків. Немає індексу або щось не збіглося — читає
звичайний zipfile, як старі проєкти.

Перенесення сітки зі старого файлу — копія стиснених байтів як є (add_raw),
без розпаковки і нового стиснення.
"""
from __future__ import annotations

import os
import struct
import time
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor

from fs_i18n import tr

CHUNK = 1 << 20          # байтів сирих даних у шматку
BATCH = 64               # шматків у памʼяті за раз (64 МіБ сирих)
MESH_LEVEL = 1
_U32 = 0xFFFFFFFF
_U16 = 0xFFFF
# Файл, більший за це, пишемо із zip64 одразу: стиснений розмір ще
# невідомий, а DEFLATE на нестисному вмісті трохи більший за вхід.
_Z64_FILE = 0xF0000000
_LOCAL = struct.Struct("<4sHHHHHLLLHH")           # 30 байтів
_CENTRAL = struct.Struct("<4sHHHHHHLLLHHHHHLL")   # 46 байтів
_EOCD = struct.Struct("<4sHHHHLLH")               # 22 байти
_EOCD64 = struct.Struct("<4sQHHLLQQQQ")           # 56 байтів
_LOC64 = struct.Struct("<4sLQL")                  # 20 байтів


def workers() -> int:
    return max(1, min(16, os.cpu_count() or 1))


def _deflate(args) -> bytes:
    data, last, level = args
    c = zlib.compressobj(level, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush(zlib.Z_FINISH if last else zlib.Z_FULL_FLUSH)


def _inflate(data: bytes) -> bytes:
    return zlib.decompressobj(-15).decompress(data)


def _dos_time(t: float | None = None) -> tuple[int, int]:
    tm = time.localtime(t)
    if tm.tm_year < 1980:
        return 0, (0 << 9) | (1 << 5) | 1
    return ((tm.tm_hour << 11) | (tm.tm_min << 5) | (tm.tm_sec // 2),
            ((tm.tm_year - 1980) << 9) | (tm.tm_mon << 5) | tm.tm_mday)


class Writer:
    """Пише ZIP запис за записом. `chunks[імʼя]` — розміри стиснених шматків
    кожного запису, стисненого тут (для індексу)."""

    def __init__(self, path, pool: ThreadPoolExecutor | None = None):
        self.fh = open(path, "wb")
        self._own = pool is None
        self.pool = pool or ThreadPoolExecutor(workers())
        self.central: list[tuple] = []
        self.chunks: dict[str, list[int]] = {}
        self._dtime, self._ddate = _dos_time()

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        try:
            if kind is None:
                self._write_central()
        finally:
            self.fh.close()
            if self._own:
                self.pool.shutdown()

    # --- записи ---------------------------------------------------------

    def _begin(self, name: str, method: int, z64: bool) -> tuple[int, bytes, int]:
        nb = name.encode("utf-8")
        flags = 0 if name.isascii() else 0x800
        offset = self.fh.tell()
        extra = struct.pack("<HHQQ", 1, 16, 0, 0) if z64 else b""
        self.fh.write(_LOCAL.pack(
            b"PK\x03\x04", 45 if z64 else 20, flags, method,
            self._dtime, self._ddate, 0,
            _U32 if z64 else 0, _U32 if z64 else 0, len(nb), len(extra)))
        self.fh.write(nb + extra)
        return offset, nb, flags

    def _end(self, offset: int, nb: bytes, flags: int, method: int, z64: bool,
             crc: int, csize: int, size: int) -> None:
        if not z64 and (csize >= _U32 or size >= _U32):
            raise OSError(tr("запис {name} виріс понад 4 ГБ",
                             name=nb.decode("utf-8", "replace")))
        end = self.fh.tell()
        self.fh.seek(offset + 14)
        if z64:
            self.fh.write(struct.pack("<LLL", crc, _U32, _U32))
            self.fh.seek(offset + 30 + len(nb) + 4)
            self.fh.write(struct.pack("<QQ", size, csize))
        else:
            self.fh.write(struct.pack("<LLL", crc, csize, size))
        self.fh.seek(end)
        self.central.append((nb, flags, method, crc, csize, size, offset, z64))

    def add_bytes(self, name: str, data: bytes, level: int = 6) -> None:
        self._add(name, len(data), lambda pos, n: data[pos:pos + n], level)

    def add_file(self, name: str, path, level: int = MESH_LEVEL) -> None:
        size = os.path.getsize(path)
        with open(path, "rb") as src:
            def read(pos, n):
                got = src.read(n)
                if len(got) != n:
                    raise OSError(tr("файл {path} змінився під час запису",
                                     path=path))
                return got
            self._add(name, size, read, level)
            if src.read(1):
                raise OSError(tr("файл {path} змінився під час запису",
                                 path=path))

    def _add(self, name: str, size: int, read, level: int) -> None:
        z64 = size >= _Z64_FILE
        offset, nb, flags = self._begin(name, zipfile.ZIP_DEFLATED, z64)
        crc = csize = pos = 0
        sizes: list[int] = []
        while True:
            jobs = []
            while len(jobs) < BATCH:
                n = min(CHUNK, size - pos)
                data = read(pos, n)
                pos += n
                crc = zlib.crc32(data, crc)
                jobs.append((data, pos >= size, level))
                if pos >= size:
                    break
            for out in self.pool.map(_deflate, jobs):
                self.fh.write(out)
                sizes.append(len(out))
                csize += len(out)
            if pos >= size:
                break
        self.chunks[name] = sizes
        self._end(offset, nb, flags, zipfile.ZIP_DEFLATED, z64, crc, csize, size)

    def add_raw(self, src, info: zipfile.ZipInfo) -> None:
        """Запис `info` з іншого архіву (`src` — відкритий файл того архіву)
        — стисненими байтами як є."""
        start = data_start(src, info)
        z64 = info.file_size >= _U32 or info.compress_size >= _U32
        offset, nb, flags = self._begin(info.filename, info.compress_type, z64)
        src.seek(start)
        left = info.compress_size
        while left:
            buf = src.read(min(left, 8 << 20))
            if not buf:
                raise OSError(tr("архів обрізаний"))
            self.fh.write(buf)
            left -= len(buf)
        self._end(offset, nb, flags, info.compress_type, z64,
                  info.CRC, info.compress_size, info.file_size)

    # --- кінець архіву --------------------------------------------------

    def _write_central(self) -> None:
        cd_start = self.fh.tell()
        for nb, flags, method, crc, csize, size, offset, z64 in self.central:
            extra = b""
            f_size, f_csize, f_off = size, csize, offset
            if z64 or offset >= _U32:
                vals = []
                if z64:
                    vals += [size, csize]
                    f_size = f_csize = _U32
                if offset >= _U32:
                    vals.append(offset)
                    f_off = _U32
                extra = struct.pack(f"<HH{len(vals)}Q", 1, 8 * len(vals), *vals)
            need = 45 if extra else 20
            self.fh.write(_CENTRAL.pack(
                b"PK\x01\x02", need, need, flags, method, self._dtime, self._ddate,
                crc, f_csize, f_size, len(nb), len(extra), 0, 0, 0, 0, f_off))
            self.fh.write(nb + extra)
        cd_end = self.fh.tell()
        n, cd_size = len(self.central), cd_end - cd_start
        if n > _U16 or cd_start >= _U32 or cd_size >= _U32:
            self.fh.write(_EOCD64.pack(b"PK\x06\x06", 44, 45, 45, 0, 0,
                                       n, n, cd_size, cd_start))
            self.fh.write(_LOC64.pack(b"PK\x06\x07", 0, cd_end, 1))
        self.fh.write(_EOCD.pack(b"PK\x05\x06", 0, 0, min(n, _U16), min(n, _U16),
                                 min(cd_size, _U32), min(cd_start, _U32), 0))


def data_start(src, info: zipfile.ZipInfo) -> int:
    """Де в архіві починаються стиснені байти запису `info`."""
    src.seek(info.header_offset)
    head = src.read(30)
    if len(head) != 30 or head[:4] != b"PK\x03\x04":
        raise OSError(tr("поганий заголовок запису {name}",
                         name=info.filename))
    nlen, elen = struct.unpack("<HH", head[26:30])
    return info.header_offset + 30 + nlen + elen


def inflate_to(src, info: zipfile.ZipInfo, sizes: list[int], out_path,
               pool: ThreadPoolExecutor) -> bool:
    """Запис `info` → файл `out_path`, шматки (`sizes` з індексу) — у кілька
    потоків. False — індекс не пасує до запису (тоді читати звичайно)."""
    if (info.compress_type != zipfile.ZIP_DEFLATED or not sizes
            or sum(sizes) != info.compress_size
            or len(sizes) != max(1, -(-info.file_size // CHUNK))):
        return False
    src.seek(data_start(src, info))
    crc = total = 0
    with open(out_path, "wb") as out:
        for b in range(0, len(sizes), BATCH):
            parts = [src.read(n) for n in sizes[b:b + BATCH]]
            try:
                datas = list(pool.map(_inflate, parts))
            except zlib.error:
                return False
            for i, data in enumerate(datas):
                last = b + i == len(sizes) - 1
                if len(data) != (info.file_size - total if last else CHUNK):
                    return False
                crc = zlib.crc32(data, crc)
                total += len(data)
                out.write(data)
    return total == info.file_size and crc == info.CRC
