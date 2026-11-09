"""Ремонт геометрії службою Windows — тим самим, чим лагодять слайсери.

Навіщо окремим модулем, а не в `fs_geometry`: це не алгоритм, а виклик чужої
служби. Усе, що з цього випливає — може не бути на цій машині, працює
хвилинами, говорить помилками COM, обмінюється даними через файловий формат —
не має протікати в геометрію.

ЩО ЦЕ. У Windows 10/11 вбудований ремонт сіток від Netfabb, доступний через
WinRT `Windows.Graphics.Printing3D`. Саме його кличуть Bambu Studio, Orca й
PrusaSlicer під кнопкою «Fix model» (`src/slic3r/Utils/FixModelByWin10.cpp`) —
і саме тому цієї кнопки в них немає на Linux і macOS.

ЧИМ КРАЩИЙ ЗА НАШ `repair_chain`. Закриває дірки, яких `vtkFillHolesFilter`
не бачить — не обведені одним замкненим контуром на manifold-поверхні, — і при
цьому НЕ проріднює модель. Виміряно на `plane7.stl` (266 788 трикутників):

    repair_chain   відкритих ребер 42 → 42   (не закрив жодної),  2.9 с
    служба Windows відкритих ребер 42 → 0,   трикутників +1.6%,   24 с
                   габарит зсунувся на 9 мкм, вивернутих частин 0

ЧИМ ГІРШИЙ. Тільки Windows. Іде десятки секунд. Тому виклик — з потоку, як і
решта ремонту (`RepairWorker`), а не з вікна.

ОБМІН ІДЕ ЧЕРЕЗ 3MF (zip з XML): іншого способу подати службі модель немає.
Читати буфери WinRT напряму не варто, хоч і коротше: `get_vertex_positions()`
віддає 24 байти на вершину попри формат R32G32B32Float, і розбір виходить
угадуванням — перший підхід дав обʼєм 9.5e39 замість 3.2e6.

ОДИНИЦІ. Сітки в програмі — у метрах, а служба ця для друку й міряє допуски в
міліметрах, як і слайсери, що її кличуть. Тому на час ремонту переводимо в
міліметри й повертаємо назад.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import sys
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import pyvista as pv

# Пакунки pywinrt. Обидва ставляться одним рядком requirements, але шукаємо
# кожен окремо: часткове встановлення краще побачити тут, ніж ImportError
# посеред ремонту.
PACKAGES = ("winrt.windows.graphics.printing3d",
            "winrt.windows.storage.streams")

M_TO_MM = 1000.0

_CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType='
    '"application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType='
    '"application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    '</Types>')
_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns='
    '"http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type='
    '"http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    '</Relationships>')


# --------------------------------------------------------------------------
# Чи є служба на цій машині
# --------------------------------------------------------------------------

def available() -> bool:
    """Чи можна взагалі кликати ремонт.

    Без важкого імпорту: перевірка робиться при побудові панелі, на кожному
    старті програми. `find_spec` дивиться, чи пакунок встановлено, і не тягне
    за собою самої бібліотеки WinRT.
    """
    if sys.platform != "win32":
        return False
    try:
        return all(importlib.util.find_spec(name) is not None
                   for name in PACKAGES)
    except (ImportError, ValueError):
        return False


def why_not() -> str:
    """Чому кнопка неактивна — людськими словами."""
    if sys.platform != "win32":
        return ("Ремонт службою Netfabb є лише у Windows: це частина самої "
                "системи, а не бібліотека програми. З тієї самої причини "
                "кнопки «Fix model» немає в Bambu Studio й PrusaSlicer на "
                "Linux і macOS. Тут лишаються «Полагодити все» й "
                "«Обгорнути…».")
    return ("Не встановлено пакунки WinRT. У теці програми:" + "\n\n"
            "    pip install winrt-Windows.Graphics.Printing3D "
            "winrt-Windows.Storage.Streams" + "\n\n"
            "Сама служба ремонту вже є у Windows — ці пакунки лише дають до "
            "неї доступ із Python.")


# --------------------------------------------------------------------------
# 3MF
# --------------------------------------------------------------------------

def _triangles(mesh: pv.PolyData) -> np.ndarray:
    faces = np.asarray(mesh.faces)
    if faces.size == 0 or faces.size % 4 or not np.all(faces[::4] == 3):
        mesh = mesh.triangulate()
        faces = np.asarray(mesh.faces)
    return faces.reshape(-1, 4)[:, 1:]


def _to_3mf(mesh: pv.PolyData) -> bytes:
    """Сітка в метрах → пакунок 3MF у міліметрах."""
    tri = _triangles(mesh)
    pts = np.asarray(mesh.points, dtype=np.float64) * M_TO_MM
    # 9 значущих цифр, а не 6: STL зберігає float32 (~7 цифр), і округлення
    # до 6 злило б сусідні вершини на дрібних деталях — тобто ми б самі
    # зробили те, чого від ремонту й хочемо уникнути.
    verts = "".join(f'<vertex x="{x:.9g}" y="{y:.9g}" z="{z:.9g}"/>'
                    for x, y, z in pts)
    faces = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>'
                    for a, b, c in tri)
    model = ('<?xml version="1.0" encoding="UTF-8"?>'
             f'<model unit="millimeter" xml:lang="en-US" xmlns="{_CORE_NS}">'
             '<resources><object id="1" type="model"><mesh>'
             f'<vertices>{verts}</vertices><triangles>{faces}</triangles>'
             '</mesh></object></resources>'
             '<build><item objectid="1"/></build></model>')
    buf = io.BytesIO()
    # compresslevel=1: пакунок живе секунди в памʼяті, і тиснути його сильно
    # означає лише марно чекати. На plane7 різниця — 4 МБ проти 3 МБ.
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model)
    return buf.getvalue()


def _tag(elem) -> str:
    return elem.tag.rsplit("}", 1)[-1]


def _mesh_of(elem) -> tuple[np.ndarray, np.ndarray]:
    pts, tri = [], []
    for kid in elem:
        if _tag(kid) == "vertices":
            pts = [(float(v.get("x")), float(v.get("y")), float(v.get("z")))
                   for v in kid]
        elif _tag(kid) == "triangles":
            tri = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3")))
                   for t in kid]
    return (np.array(pts, dtype=np.float64).reshape(-1, 3),
            np.array(tri, dtype=np.int64).reshape(-1, 3))


def _matrix(spec: str | None) -> np.ndarray:
    """`transform` із 3MF як 4×4. Формат — 12 чисел: 3×3 і зсув останнім
    рядком, бо точки в 3MF множаться рядком-вектором зліва."""
    if not spec:
        return np.eye(4)
    values = [float(v) for v in spec.split()]
    if len(values) != 12:
        return np.eye(4)
    out = np.eye(4)
    out[:3, :3] = np.array(values[:9]).reshape(3, 3)
    out[3, :3] = values[9:]
    return out


def _apply(pts: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    if np.array_equal(matrix, np.eye(4)):
        return pts
    return pts @ matrix[:3, :3] + matrix[3, :3]


def _from_3mf(data: bytes) -> pv.PolyData:
    """Пакунок 3MF у міліметрах → сітка в метрах.

    Розбирати доводиться і складені обʼєкти: службі ми подаємо одне тіло
    однією сіткою, а вона віддає сітку обʼєктом `1` і додає обгортку —
    обʼєкт `2` з єдиним `<component objectid="1"/>`, і вже його ставить у
    `<build>`. Хто читає лише `<item>` і сподівається знайти там сітку,
    отримує порожньо.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".model")]
        if not names:
            raise RuntimeError("служба повернула пакунок без моделі")
        root = ET.fromstring(z.read(names[0]))

    meshes: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    parts: dict[str, list[tuple[str, np.ndarray]]] = {}
    for obj in root.iter():
        if _tag(obj) != "object":
            continue
        for kid in obj:
            if _tag(kid) == "mesh":
                meshes[obj.get("id")] = _mesh_of(kid)
            elif _tag(kid) == "components":
                parts[obj.get("id")] = [
                    (c.get("objectid"), _matrix(c.get("transform")))
                    for c in kid if _tag(c) == "component"]

    placed: list[tuple[np.ndarray, np.ndarray]] = []

    def collect(oid: str, matrix: np.ndarray, depth: int = 0) -> None:
        if depth > 16:
            raise RuntimeError("у моделі кільце посилань між обʼєктами")
        if oid in meshes:
            pts, tri = meshes[oid]
            placed.append((_apply(pts, matrix), tri))
        for ref, own in parts.get(oid, ()):
            # Своє перетворення складника йде ПЕРШИМ: спершу він стає на
            # місце в батькові, і вже батько — на місце в сцені.
            collect(ref, own @ matrix, depth + 1)

    items = [it for it in root.iter() if _tag(it) == "item"]
    if items:
        for item in items:
            collect(item.get("objectid"), _matrix(item.get("transform")))
    else:
        for oid in meshes:
            collect(oid, np.eye(4))
    placed = [(p, t) for p, t in placed if len(p) and len(t)]
    if not placed:
        raise RuntimeError("служба повернула порожню модель")

    offset, all_pts, all_tri = 0, [], []
    for pts, tri in placed:
        all_pts.append(pts)
        all_tri.append(tri + offset)
        offset += len(pts)
    pts = np.vstack(all_pts) / M_TO_MM
    tri = np.vstack(all_tri)
    return pv.PolyData(
        pts, np.hstack([np.full((len(tri), 1), 3, np.int64), tri]).ravel())


# --------------------------------------------------------------------------
# Сам ремонт
# --------------------------------------------------------------------------

async def _run(payload: bytes, say) -> bytes:
    from winrt.windows.graphics.printing3d import Printing3D3MFPackage
    from winrt.windows.storage.streams import (
        DataReader, DataWriter, InMemoryRandomAccessStream)

    async def as_stream(data: bytes):
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream.get_output_stream_at(0))
        writer.write_bytes(data)
        await writer.store_async()
        await writer.flush_async()
        stream.seek(0)
        return stream

    async def to_bytes(stream) -> bytes:
        stream.seek(0)
        size = int(stream.size)
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(size)
        out = bytearray(size)
        # read_bytes заповнює масив НА МІСЦІ й повертає None — не bytes.
        reader.read_bytes(out)
        return bytes(out)

    package = Printing3D3MFPackage()
    model = await package.load_model_from_package_async(
        await as_stream(payload))
    say("ремонт службою Windows — на 250 тис. трикутників близько пів хвилини")
    await model.repair_async()
    say("читаю полагоджену модель")
    await package.save_model_to_package_async(model)
    return await to_bytes(await package.save_async())


def repair(mesh: pv.PolyData, say=None) -> pv.PolyData:
    """Полагоджена копія `mesh`. Сітка на вході й на виході — в метрах.

    Викликати з потоку: служба працює десятки секунд. Свого циклу подій тут
    немає, тому `asyncio.run` — а не приєднання до чужого.
    """
    say = say or (lambda _text: None)
    if mesh is None or mesh.n_cells == 0:
        raise ValueError("Нема чого лагодити: у моделі жодного трикутника.")
    if not available():
        raise RuntimeError(why_not())

    say("готую пакунок для служби ремонту")
    payload = _to_3mf(mesh)
    try:
        out = _from_3mf(asyncio.run(_run(payload, say)))
    except OSError as exc:
        # Служба говорить кодами COM. Текст у них буває англійський і
        # невиразний, тому лишаємо його поруч, а не замість пояснення.
        raise RuntimeError(
            "Служба ремонту Windows відмовила: "
            f"{getattr(exc, 'strerror', None) or exc}." + "\n\n"
            "Найчастіше це завелика модель або вимкнена служба. "
            "Спробуйте спершу «Проріднити…», або скористайтеся "
            "«Полагодити все» чи «Обгорнути…».") from exc
    if out.n_cells == 0:
        raise RuntimeError("Після ремонту не лишилося жодного трикутника.")
    return out
