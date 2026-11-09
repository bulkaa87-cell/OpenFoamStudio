"""Швидке читання полів OpenFOAM: Rust (foamrs), а без нього — numpy.

Навіщо. Анімація читає ДЕСЯТКИ моментів часу. Через VTK це ≈ 2 с на
момент на 1.4 млн комірок (filled_holes1), бо читач розбирає сітку й усі
поля в одному потоці. foamrs читає поле U того ж кейсу за 0.02 с, сім
полів паралельно — за 0.05 с (2026-11-08).

foamrs — модуль на Rust (rust/foamrs, збирає tools/build_rust.ps1). Його
може й не бути: тоді те саме робить запасне читання на numpy нижче, лише
повільніше на текстових полях. Програма від Rust не залежить.

Порядок комірок — той самий, що й у VTK (перевірено на filled_holes1:
1 371 933 комірки, збіг до точності float32), поки кількість комірок
однакова. Інакше той, хто кличе, мусить читати кадр через VTK.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

try:                                       # pragma: no cover - залежить від збірки
    import foamrs as _rs
except ImportError:                        # pragma: no cover
    _rs = None


def available() -> bool:
    """Чи є швидкий модуль на Rust."""
    return _rs is not None


def has(name: str) -> bool:
    """Чи вміє зібраний foamrs функцію `name` (старий .pyd може не вміти)."""
    return _rs is not None and hasattr(_rs, name)


def surface_lines(points, tris, vectors, normals, seeds, max_length: float,
                  *, max_faces: int = 20000, offset: float = 0.0,
                  floor: float = 0.0, spacing: float = 0.0,
                  min_length: float = 0.0):
    """Лінії по поверхні з трикутників (Rust). (точки (K,3), довжини ліній).

    Запасного шляху тут немає: його тримає той, хто кличе (fs_views —
    vtkStreamTracer), бо numpy-версія вийшла б повільнішою за обидва.
    """
    if not has("surface_lines"):
        raise RuntimeError(why_not() or "foamrs без surface_lines — "
                           "перезберіть tools\\build_rust.ps1")
    return _rs.surface_lines(
        np.ascontiguousarray(points, dtype=np.float64),
        np.ascontiguousarray(tris, dtype=np.int64),
        np.ascontiguousarray(vectors, dtype=np.float64),
        np.ascontiguousarray(normals, dtype=np.float64),
        np.ascontiguousarray(seeds, dtype=np.int64),
        float(max_length), int(max_faces), float(offset), float(floor),
        float(spacing), float(min_length))


def why_not() -> str:
    if _rs is not None:
        return ""
    return ("Модуль foamrs (Rust) не зібрано — поля читаються запасним "
            "способом, повільніше. Зібрати: tools\\build_rust.ps1.")


# --------------------------------------------------------------------------
# Запасне читання на numpy — той самий формат, що й у foamrs.
# --------------------------------------------------------------------------

_NCOMP = {b"scalar": 1, b"vector": 3, b"symmTensor": 6, b"tensor": 9}
_NUM = re.compile(rb"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _binary(buf: bytes) -> bool:
    head = buf[:buf.find(b"}") + 1] if b"}" in buf[:4096] else buf[:4096]
    return b"binary" in head


def _entry(buf: bytes, at: int, binary: bool, n_uniform: int):
    m = re.compile(rb"\s*(uniform|nonuniform)\s*").match(buf, at)
    if not m:
        raise ValueError("expected uniform/nonuniform")
    i = m.end()
    if m.group(1) == b"uniform":
        if buf[i:i + 1] == b"(":
            close = buf.index(b")", i)
            v = np.array([float(x) for x in buf[i + 1:close].split()])
        else:
            v = np.array([float(_NUM.match(buf, i).group(0))])
        return (np.tile(v, (n_uniform, 1)) if v.size > 1
                else np.full(n_uniform, v[0]))
    m = re.compile(rb"List<(\w+)>\s*(\d+)\s*\(").match(buf, i)
    if not m:
        raise ValueError("expected List<type> N (")
    ncomp = _NCOMP[m.group(1)]
    n = int(m.group(2))
    body = m.end()
    if binary:
        data = np.frombuffer(buf, dtype="<f8", count=n * ncomp, offset=body)
        data = data.copy()
    else:
        # Дужки векторів прибираємо разом: лишаються самі числа.
        end, depth = body, 1
        while depth:
            c = buf[end:end + 1]
            depth += (c == b"(") - (c == b")")
            end += 1
        flat = buf[body:end - 1].replace(b"(", b" ").replace(b")", b" ")
        data = np.array(flat.split(), dtype=float)
    return data if ncomp == 1 else data.reshape(n, ncomp)


def _internal_py(path: str, n_cells: int = 0):
    buf = Path(path).read_bytes()
    at = buf.index(b"internalField") + len(b"internalField")
    return _entry(buf, at, _binary(buf), n_cells)


def _patch_py(buf: bytes, binary: bool, name: str, n_faces: int):
    """Значення патча — лише для текстових полів: у бінарних шукати
    блок регуляркою небезпечно (у сирих байтах буває що завгодно)."""
    if binary:
        raise ValueError("binary patch values need foamrs")
    m = re.compile(rb"\n\s*" + re.escape(name.encode()) + rb"\s*\{").search(
        buf, buf.find(b"boundaryField"))
    if not m:
        return None
    end = buf.index(b"}", m.end())
    v = re.compile(rb"\bvalue\b").search(buf, m.end(), end)
    if not v:
        return None
    return _entry(buf, v.end(), False, n_faces)


def _one_py(path: str, n_cells: int, patches, internal: bool):
    buf = Path(path).read_bytes()
    binary = _binary(buf)
    inner = None
    if internal:
        at = buf.index(b"internalField") + len(b"internalField")
        inner = _entry(buf, at, binary, n_cells)
    out = {}
    for name, n in patches:
        try:
            out[name] = _patch_py(buf, binary, name, n)
        except (ValueError, KeyError):
            out[name] = None
    return inner, out


# --------------------------------------------------------------------------
# Те, чим користується програма.
# --------------------------------------------------------------------------

def read_internal(path: str, n_cells: int = 0) -> np.ndarray:
    """Внутрішнє поле одного файлу: (N,) або (N, 3)."""
    if _rs is not None:
        return _rs.read_internal(str(path), int(n_cells))
    return _internal_py(str(path), n_cells)


def read_fields(paths, n_cells: int = 0, patches=(), internal: bool = True):
    """Багато файлів разом: [(внутрішнє або None, {патч: масив або None})].

    З foamrs — паралельно на всіх ядрах і поза GIL. Без нього — потоками
    Python: читання файлу й np.frombuffer GIL відпускають, тож бінарні
    поля і так читаються паралельно; текстові — повільніше.
    """
    paths = [str(p) for p in paths]
    patches = [(str(n), int(k)) for n, k in patches]
    if _rs is not None:
        return _rs.read_fields(paths, int(n_cells), patches, bool(internal))
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
        return list(pool.map(
            lambda p: _one_py(p, n_cells, patches, internal), paths))


def n_cells(case_dir: str, time_dir: str = "") -> int:
    """Кількість комірок сітки з заголовка owner (рядок `note`), або 0.

    Сітка в теці моменту часу (рухома чи адаптивна) важливіша за ту, що в
    constant: саме вона стоїть під полями цього моменту.
    """
    for root in ([Path(case_dir) / time_dir] if time_dir else []) + [
            Path(case_dir) / "constant"]:
        owner = root / "polyMesh" / "owner"
        if not owner.is_file():
            continue
        with open(owner, "rb") as fh:
            head = fh.read(2048)
        m = re.search(rb"nCells:\s*(\d+)", head)
        if m:
            return int(m.group(1))
    return 0


def read_labels(path: str) -> np.ndarray:
    """labelList (owner, neighbour…): текстовий або бінарний, label=32."""
    buf = Path(path).read_bytes()
    head_end = buf.find(b"}") + 1
    if b"label=64" in buf[:head_end]:
        dtype = "<i8"
    else:
        dtype = "<i4"
    m = re.compile(rb"\n\s*(\d+)\s*\(").search(buf, head_end)
    if not m:
        raise ValueError(f"{path}: no labelList")
    n, body = int(m.group(1)), m.end()
    if _binary(buf):
        return np.frombuffer(buf, dtype=dtype, count=n, offset=body).astype(
            np.int64)
    end = buf.index(b")", body)
    return np.array(buf[body:end].split(), dtype=np.int64)


def boundary(case_dir: str, time_dir: str = "") -> dict[str, tuple[int, int]]:
    """{патч: (startFace, nFaces)} з polyMesh/boundary — він завжди текстовий."""
    for root in ([Path(case_dir) / time_dir] if time_dir else []) + [
            Path(case_dir) / "constant"]:
        f = root / "polyMesh" / "boundary"
        if not f.is_file():
            continue
        text = f.read_text(errors="replace")
        out = {}
        # Імʼя патча — будь-що без пробілів і дужок: у snappy бувають
        # «body_part-1.stl» тощо, і \w таке пропускав.
        for m in re.finditer(r"\n\s*([^\s{}()\";]+)\s*\{([^}]*)\}", text):
            body = m.group(2)
            n = re.search(r"nFaces\s+(\d+)", body)
            s = re.search(r"startFace\s+(\d+)", body)
            if n and s:
                out[m.group(1)] = (int(s.group(1)), int(n.group(1)))
        return out
    return {}


def patch_owner_cells(case_dir: str, patches, time_dir: str = "") -> dict:
    """{патч: номери комірок, яким належать його грані} — за порядком граней.

    Потрібно для патчів без `value` у файлі (zeroGradient на стінці): тоді
    значення на грані — це значення комірки за нею. Саме так рахує й VTK.
    """
    bnd = boundary(case_dir, time_dir)
    root = Path(case_dir) / time_dir if time_dir and (
        Path(case_dir) / time_dir / "polyMesh" / "owner").is_file() \
        else Path(case_dir) / "constant"
    owner = read_labels(str(root / "polyMesh" / "owner"))
    out = {}
    for name in patches:
        if name in bnd:
            s, n = bnd[name]
            out[name] = owner[s:s + n]
    return out


def mesh_moves(case_dir: str, times) -> bool:
    """Чи має хоч один із цих моментів власну сітку (рух чи адаптація).

    Тоді кадри не можна збирати за однією таблицею комірок: їхня кількість
    і порядок від моменту до моменту різні.
    """
    base = Path(case_dir)
    return any((base / t / "polyMesh").is_dir() for t in times)
