"""Завантаження STL, діагностика і підготовка поверхні для snappyHexMesh.

Діагностика свідомо повторює те, що показує `surfaceCheck` з OpenFOAM, щоб
проблеми було видно ДО того, як запускати меш: відкриті ребра, non-manifold
ребра, вироджені трикутники, кількість незвʼязаних частин, узгодженість
нормалей і замкненість.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pyvista as pv
import vtk
import fs_fmt as fmt
from fs_i18n import tr


# --------------------------------------------------------------------------
# Результат діагностики
# --------------------------------------------------------------------------

@dataclass
class Diagnosis:
    n_triangles: int = 0
    n_points: int = 0
    open_edges: int = 0
    non_manifold_edges: int = 0
    degenerate: int = 0
    parts: int = 1
    area: float = 0.0
    volume: float = 0.0
    signed_volume: float = 0.0
    bounds: tuple[float, ...] = (0, 0, 0, 0, 0, 0)
    min_edge: float = 0.0
    max_edge: float = 0.0
    median_edge: float = 0.0
    # Медіана НАЙКОРОТШОГО ребра кожного трикутника. Саме вона каже, наскільки
    # дрібно STL описує ФОРМУ, а median_edge — ні.
    #
    # На лофтованому крилі трикутники витягнуті: короткі впоперек хорди, довгі
    # вздовж розмаху. Медіана всіх ребер тоді показує крок між перерізами —
    # величину, що до форми профілю не має стосунку. Вийде «сітка й так
    # дрібніша за трикутники» там, де передня кромка насправді описана з
    # кроком у міліметри.
    short_edge: float = 0.0
    flipped_faces: int = 0      # граней у вивернутих частинах
    inverted_parts: int = 0     # скільки частин вивернуто навиворіт
    bad_winding: int = 0        # ребер, де сусідні трикутники намотані однаково
    # Маска граней вивернутих частин — щоб показати їх у вʼюпорті, не рахуючи
    # все вдруге. Не для друку: це масив на сотні тисяч елементів.
    inverted_cells: object = field(default=None, repr=False)
    projected: tuple[float, float, float] = (0.0, 0.0, 0.0)  # силует по X, Y, Z
    hole_sizes: list[float] = field(default_factory=list)  # діагоналі, спадно
    notes: list[tuple[str, str]] = field(default_factory=list)  # (рівень, текст)

    @property
    def closed(self) -> bool:
        return self.open_edges == 0

    @property
    def largest_hole(self) -> float:
        return self.hole_sizes[0] if self.hole_sizes else 0.0

    @property
    def flipped_fraction(self) -> float:
        return self.flipped_faces / self.n_triangles if self.n_triangles else 0.0

    @property
    def frontal(self) -> float:
        """Міделевий переріз: силует із напрямку потоку (+X)."""
        return self.projected[0]

    @property
    def planform(self) -> float:
        """Площа в плані: силует згори. Для літака це опорна площа крила."""
        return self.projected[2]

    @property
    def streamwise_axis(self) -> int:
        """Вісь, уздовж якої силует найменший — найімовірніший напрямок потоку."""
        if not any(self.projected):
            return 0
        return min(range(3), key=lambda i: self.projected[i])

    @property
    def normals_outward(self) -> bool:
        return self.signed_volume > 0

    @property
    def size(self) -> tuple[float, float, float]:
        b = self.bounds
        return (b[1] - b[0], b[3] - b[2], b[5] - b[4])

    @property
    def diagonal(self) -> float:
        sx, sy, sz = self.size
        return math.sqrt(sx * sx + sy * sy + sz * sz)


# --------------------------------------------------------------------------
# Завантаження
# --------------------------------------------------------------------------

def _flip(mesh: pv.PolyData) -> pv.PolyData:
    """Вивертає орієнтацію граней. У pyvista 0.49 flip_normals прибрали."""
    if hasattr(mesh, "flip_faces"):
        return mesh.flip_faces(inplace=False)
    return mesh.flip_normals()


def _require_cells(out: pv.PolyData, name: str, why: str = "") -> pv.PolyData:
    """Не віддавати порожню поверхню замість результату.

    Фільтри VTK на неприйнятному вході не кидають винятку: вони пишуть
    зауваження у своє вікно виводу й повертають нуль комірок. Перше таке
    місце знайшлося на «Згустити» — модель із дублікатами граней зникала
    молча, без жодного слова у вікні. Порожній результат — це збій, і
    називати його треба збоєм.
    """
    if out is None or out.n_cells == 0:
        raise ValueError(tr("“{name}” gave no result — the surface came "
                            "out empty.{v}", name=name, v=" " + why if why else ""))
    return out


def drop_stray_cells(mesh: pv.PolyData) -> pv.PolyData:
    """Лишає саму поверхню: без окремих точок і ліній.

    `clean` з `polys_to_lines`/`lines_to_points` перетворює вироджені
    трикутники не на ніщо, а на лінії й точки, і вони лишаються в тій самій
    сітці. Для поверхні це сміття, яке ще й збиває лічильники: `n_cells`
    рахує їх усі, а `faces` містить лише многокутники, і будь-яке зіставлення
    «грань -> комірка» після цього розʼїжджається.
    """
    # GetNumberOfPolys, а не n_faces: у pyvista до 0.46 `n_faces` — це
    # застаріла назва для n_cells, і перевірка мовчки нічого б не ловила.
    if int(mesh.n_cells) == int(mesh.GetNumberOfPolys()):
        return mesh
    out = pv.PolyData(mesh.points, mesh.faces)
    # Без явних прапорців clean сам перетворив би вироджені трикутники назад
    # на лінії — те саме сміття, від якого тут і рятуємось.
    return out.clean(point_merging=False, lines_to_points=False,
                     polys_to_lines=False, strips_to_polys=False)


# --------------------------------------------------------------------------
# Формати файлів сітки: спільний код MeshFix Studio
# --------------------------------------------------------------------------
# FILE FORMATS. The readers and writers of STL, 3MF, OBJ, PLY, OFF, glTF and
# VTK are in the shared package (meshfix.formats and meshfix.check of
# meshfix-core 0.3.0 and later, see fs_shared.py): one code for both
# programs. With an older package OpenFOAM Studio reads what pv.read reads
# and writes STL only. constant/triSurface always gets STL (write_stl):
# snappyHexMesh reads it.

def formats():
    """meshfix.check of the shared package when it has the file formats
    (save_mesh, meshfix-core 0.3.0 and later), else None."""
    try:
        import fs_shared
        chk = fs_shared.module("meshfix.check")
    except ImportError:
        return None
    return chk if hasattr(chk, "save_mesh") else None


SURFACE_SUFFIXES_OLD = (".stl", ".obj", ".ply", ".vtp", ".vtk")


def surface_suffixes() -> tuple:
    """The suffixes of the mesh files that the program opens."""
    chk = formats()
    return tuple(chk.READERS) if chk else SURFACE_SUFFIXES_OLD


def open_filter() -> str:
    """The filter of an open dialog for mesh files."""
    chk = formats()
    if chk:
        return chk.open_filter()
    return tr("Meshes ({patterns});;All files (*)",
              patterns=" ".join("*" + s for s in SURFACE_SUFFIXES_OLD))


def unit_in_file(path) -> bool:
    """True when the format of `path` keeps its unit (3MF, glTF): then the
    program does not need to ask for the units."""
    chk = formats()
    if not chk:
        return False
    try:
        return chk.format_for(path).unit is not None
    except ValueError:
        return False


def load_surface(path: str) -> pv.PolyData:
    """Читає файл сітки і повертає трикутну поверхню.

    Файл без жодного трикутника відкидаємо ТУТ, а не десь далі. Перевірка
    «немає комірок» його не ловить: у хмари точок і в каркаса комірки є, і
    така геометрія доходила до вʼюпорта, де падала на порахунку нормалей —
    уже у вікні, з трасуванням у консоль замість людського повідомлення.

    With meshfix-core 0.3.0 and later: meshfix.check.load_surface (the same
    code, with the readers of meshfix.formats: 3MF, OFF, glTF too).
    """
    chk = formats()
    if chk:
        return chk.load_surface(path)
    mesh = pv.read(path)
    if not isinstance(mesh, pv.PolyData):
        mesh = mesh.extract_surface(algorithm="dataset_surface")
    mesh = drop_stray_cells(mesh.triangulate())
    if int(mesh.GetNumberOfPolys()) == 0:
        raise ValueError(
            tr("The file has no triangles: {n_points} points, {n_lines} "
               "lines, {n_verts} separate vertices. This is a point "
               "cloud or a wireframe, not a surface — there is nothing "
               "for the flow to go around here.", n_points=mesh.n_points, n_lines=mesh.n_lines, n_verts=mesh.n_verts))
    return mesh


def guess_scale(mesh: pv.PolyData) -> tuple[float, str]:
    """Здогадка про одиниці. STL безрозмірний, а OpenFOAM працює в метрах.

    Повертає (множник, пояснення). Евристика проста: типова деталь у метрах
    має габарит 0.01..30, у міліметрах — сотні й тисячі.

    A 3MF or glTF file gives its unit (meshfix.check.guess_scale).
    """
    chk = formats()
    if chk and chk.UNIT in mesh.field_data:
        return chk.guess_scale(mesh)
    b = mesh.bounds
    span = max(b[1] - b[0], b[3] - b[2], b[5] - b[4])
    if span > 300:
        return (0.001, tr("overall size {span:.0f} — most likely millimeters", span=span))
    if span > 30:
        return (0.001, tr("overall size {span:.0f} — maybe millimeters", span=span))
    if span < 0.002:
        return (1000.0, tr("overall size {span:.5g} — maybe kilometers or an error", span=span))
    return (1.0, tr("overall size {span:.4g} — looks like meters", span=span))


# --------------------------------------------------------------------------
# Діагностика
# --------------------------------------------------------------------------

def _count_edges(mesh: pv.PolyData, **kw) -> int:
    opts = dict(boundary_edges=False, feature_edges=False,
                manifold_edges=False, non_manifold_edges=False)
    opts.update(kw)
    edges = mesh.extract_feature_edges(**opts)
    return int(edges.n_cells)


def cap_metrics(mesh: pv.PolyData) -> tuple[float, float]:
    """Площа й периметр кришки двигуна: (м², м).

    Кришка — відкрита поверхня, тож периметр — це довжина її краю, тобто
    відкритих ребер. З цих двох чисел виходить гідравлічний діаметр
    4·A/P, а з нього — масштаб турбулентності струменя.

    Нуль периметра означає, що краю немає: поверхня замкнена. Тоді
    повертаємо периметр кола тієї самої площі — для круглої кришки це
    точно, для інших — розумна оцінка.
    """
    if mesh is None or mesh.n_cells == 0:
        return 0.0, 0.0
    # Спершу злити однакові точки: інакше кожен шов між трикутниками, що
    # не ділять вершин, рахувався б «краєм», і периметр виходив би в
    # десятки разів більшим (так і було на диску з точкою в центрі).
    mesh = mesh.clean(tolerance=0.0)
    area = float(mesh.area)
    edges = mesh.extract_feature_edges(boundary_edges=True,
                                       feature_edges=False,
                                       manifold_edges=False,
                                       non_manifold_edges=False)
    perimeter = 0.0
    if edges.n_cells:
        lines = edges.lines.reshape(-1, 3)[:, 1:]
        pts = np.asarray(edges.points)
        perimeter = float(np.linalg.norm(pts[lines[:, 0]] - pts[lines[:, 1]],
                                         axis=1).sum())
    if perimeter <= 0 and area > 0:
        perimeter = 2.0 * math.pi * math.sqrt(area / math.pi)
    return area, perimeter


def area_vector(mesh: pv.PolyData) -> np.ndarray:
    """Σ n·dA поверхні — у тому напрямку, у який дивляться її нормалі в STL.

    Для пласкої кришки це площа на нормаль. Для кривої — менше за площу:
    частина нормалей дивиться вбік і взаємно гаситься. Саме цей вектор, а
    не «площа × одна нормаль», показує, куди насправді дме або звідки
    смокче крива поверхня.

    Нормалі НЕ перераховуються й не вирівнюються: орієнтація STL — це й є
    вибір людини, який бік порталу активний (Q27).
    """
    if mesh is None or mesh.n_cells == 0:
        return np.zeros(3)
    tri = mesh if _tri_faces(mesh) is not None else mesh.triangulate()
    faces = _tri_faces(tri)
    if faces is None:
        return np.zeros(3)
    p = np.asarray(tri.points, dtype=float)
    a, b, c = p[faces[:, 0]], p[faces[:, 1]], p[faces[:, 2]]
    return 0.5 * np.cross(b - a, c - a).sum(axis=0)


def portal_slab(mesh: pv.PolyData, thickness: float) -> pv.PolyData:
    """Поверхня порталу, видавлена на `thickness` в активний бік.

    Виходить замкнена «плитка». topoSet бере комірки, чиї центри всередині
    неї, — це й є шар джерел. Видавлюємо вздовж СЕРЕДНЬОЇ нормалі: для
    злегка кривої поверхні товщина шару трохи гуляє, але шар лишається
    з того самого боку.

    Нормалі плитки вирівнюються назовні: `surfaceToCell` із
    `useSurfaceOrientation` розрізняє «всередині» саме за ними.
    """
    s = area_vector(mesh)
    n = s / (np.linalg.norm(s) or 1.0)
    surf = mesh.clean(tolerance=0.0).triangulate()
    slab = surf.extrude(tuple(float(x) * thickness for x in n), capping=True)
    slab = slab.triangulate().clean()
    return slab.compute_normals(cell_normals=True, point_normals=False,
                                auto_orient_normals=True,
                                consistent_normals=True)


def cut_under_cap(body: pv.PolyData, cap: pv.PolyData,
                  tolerance: float) -> tuple[pv.PolyData, int]:
    """Прибрати з тіла трикутники, що лежать ПІД кришкою (Q29).

    Кришка, притулена до торця мотора без зазору, лежить в одній площині з
    його гранню. Дві поверхні на тому самому місці — і snappy не знає, якій
    віддати грані, тож патч кришки вийшов би клаптями. Тому під кришкою
    тіло «з'їдається»: трикутник іде геть, якщо його центр ближчий до
    кришки за `tolerance` і він лежить майже паралельно до неї.

    Край вирізу виходить по трикутниках тіла, тобто зубчастим. Це не
    шкодить: щілини, менші за комірку, snappy не бачить.

    Повертає (нове тіло, скільки трикутників прибрано).
    """
    if body is None or cap is None or body.n_cells == 0 or cap.n_cells == 0:
        return body, 0
    tri = body.triangulate()
    centres = np.asarray(tri.cell_centers().points)
    cap_t = cap.triangulate()
    _, closest = cap_t.find_closest_cell(centres, return_closest_point=True)
    dist = np.linalg.norm(centres - closest, axis=1)
    near = dist < tolerance
    if not near.any():
        return body, 0
    faces = _tri_faces(tri)
    p = np.asarray(tri.points, dtype=float)
    n_body = np.cross(p[faces[:, 1]] - p[faces[:, 0]],
                      p[faces[:, 2]] - p[faces[:, 0]])
    n_body /= np.linalg.norm(n_body, axis=1, keepdims=True) + 1e-30
    s = area_vector(cap_t)
    n_cap = s / (np.linalg.norm(s) or 1.0)
    flat = np.abs(n_body @ n_cap) > 0.95
    drop = near & flat
    if not drop.any():
        return body, 0
    kept = tri.extract_cells(np.flatnonzero(~drop)).extract_surface(
        algorithm="dataset_surface")
    return kept.clean(tolerance=0.0), int(drop.sum())


def _tri_faces(mesh: pv.PolyData) -> np.ndarray | None:
    """Трикутники як (N, 3) або None, якщо поверхня не трикутна."""
    faces = mesh.faces
    if faces.size == 0 or faces.size % 4:
        return None
    faces = faces.reshape(-1, 4)
    if not np.all(faces[:, 0] == 3):
        return None
    return faces[:, 1:]


def _count_regions(mesh: pv.PolyData) -> int:
    conn = vtk.vtkPolyDataConnectivityFilter()
    conn.SetInputData(mesh)
    conn.SetExtractionModeToAllRegions()
    conn.Update()
    return int(conn.GetNumberOfExtractedRegions())


def _region_points(poly: pv.PolyData, limit: int | None = None
                   ) -> list[np.ndarray] | None:
    """The points of each connected region of `poly`, in the order of VTK.

    One place for vtkPolyDataConnectivityFilter on points (holes, flipped
    parts; T51). Its RegionId is a POINT array of ITS OUTPUT: read it with
    the points of the output, never with the indexes of the input (see
    `_region_ids` for the faces). None: VTK gave no RegionId. `limit`: only
    the first regions.
    """
    conn = vtk.vtkPolyDataConnectivityFilter()
    conn.SetInputData(poly)
    conn.SetExtractionModeToAllRegions()
    conn.SetColorRegions(True)
    conn.Update()
    labelled = pv.wrap(conn.GetOutput())
    if "RegionId" not in labelled.point_data:
        return None
    ids = np.asarray(labelled.point_data["RegionId"])
    pts = labelled.points
    n = int(conn.GetNumberOfExtractedRegions())
    if limit is not None:
        n = min(n, limit)
    return [pts[ids == region] for region in range(n)]


_CELL_TAG = "_fs_cell"


def _region_ids(mesh: pv.PolyData) -> tuple[int, np.ndarray]:
    """(скільки незвʼязаних частин, номер частини для кожної грані).

    ПАСТКА, на якій легко втратити день: vtkPolyDataConnectivityFilter віддає
    точки й грані В ІНШОМУ ПОРЯДКУ, ніж отримав. Читати його RegionId по
    індексах вхідної сітки не можна — номери лягають не на ті грані. На
    реальній моделі це давало 22 633 трикутники, чиї три вершини нібито
    належать різним частинам, і «ремонт нормалей» вивертав половину деталі.

    Тому кожній грані ще до фільтра чіпляємо її власний номер (cell_data
    фільтр переносить чесно) і вертаємо результат у вхідний порядок.
    """
    rs = _rust_mesh()
    arrays = _pure_triangles(mesh) if rs is not None else None
    if arrays is not None:
        # Rust: ті самі частини (грані зі спільною вершиною) і та сама
        # нумерація — за першою гранню, як у VTK; уже у вхідному порядку.
        r = rs.mesh_check(*arrays)
        return int(r["n_regions"]), np.asarray(r["regions"], dtype=np.int64)
    zero = np.zeros(int(mesh.n_cells), dtype=np.int64)
    if int(mesh.GetNumberOfPolys()) != int(mesh.n_cells):
        # У сітці є ще й точкові або лінійні комірки: нумерація граней і
        # комірок розходиться, зіставляти їх навмання не можна.
        return _count_regions(mesh), zero
    probe = pv.PolyData(mesh.points, mesh.faces)
    probe.cell_data[_CELL_TAG] = np.arange(probe.n_cells, dtype=np.int64)

    conn = vtk.vtkPolyDataConnectivityFilter()
    conn.SetInputData(probe)
    conn.SetExtractionModeToAllRegions()
    conn.SetColorRegions(True)
    conn.Update()
    n = int(conn.GetNumberOfExtractedRegions())

    out = pv.wrap(conn.GetOutput())
    faces_out = _tri_faces(out)
    if (faces_out is None or "RegionId" not in out.point_data
            or _CELL_TAG not in out.cell_data):
        return n, zero
    ids = np.asarray(out.point_data["RegionId"])
    orig = np.asarray(out.cell_data[_CELL_TAG])
    if orig.size != mesh.n_cells or ids.size != out.n_points:
        return n, zero
    regions = zero.copy()
    regions[orig] = ids[faces_out[:, 0]]
    return n, regions


def _part_volumes(mesh: pv.PolyData, regions: np.ndarray,
                  n_regions: int) -> np.ndarray:
    """Знаковий обʼєм кожної частини. Мінус — частина вивернута навиворіт."""
    faces = _tri_faces(mesh)
    if faces is None or n_regions == 0:
        return np.zeros(max(n_regions, 0))
    p = mesh.points
    v0, v1, v2 = p[faces[:, 0]], p[faces[:, 1]], p[faces[:, 2]]
    vol = np.einsum("ij,ij->i", v0, np.cross(v1, v2)) / 6.0
    return np.bincount(regions, weights=vol, minlength=n_regions)


def inverted_mask(mesh: pv.PolyData, regions: np.ndarray | None = None,
                  n_regions: int = 0) -> np.ndarray:
    """Маска граней, що належать вивернутим навиворіт частинам.

    Саме ця перевірка, а не узгодженість намотки, ловить типову ваду збірки
    з CAD: кожна деталь усередині себе намотана бездоганно, але частина з них
    дивиться нормалями всередину. У Blender цього не видно — його вʼюпорт
    світить із двох боків, тому вивернута деталь виглядає нормальною.
    """
    if regions is None:
        n_regions, regions = _region_ids(mesh)
    vols = _part_volumes(mesh, regions, n_regions)
    bad = np.where(vols < 0)[0]
    if bad.size == 0:
        return np.zeros(int(mesh.n_cells), dtype=bool)
    return np.isin(regions, bad)


def inverted_part_count(mesh: pv.PolyData) -> tuple[int, int]:
    """(скільки частин вивернуто, скільки в них граней).

    Те саме, що робить повна діагностика, але без решти її роботи: для
    позначки біля моделі в списку потрібні лише ці два числа.
    """
    n_regions, regions = _region_ids(mesh)
    vols = _part_volumes(mesh, regions, n_regions)
    bad = np.where(vols < 0)[0]
    if bad.size == 0:
        return (0, 0)
    return (int(bad.size), int(np.count_nonzero(np.isin(regions, bad))))


def _bad_winding_edges(mesh: pv.PolyData) -> int:
    """Ребра, де два сусідні трикутники обходяться в ОДИН бік.

    Це і є справжня неузгодженість намотки: у нормально зшитій поверхні
    спільне ребро двох трикутників кожен із них проходить у свій бік. Рахуємо
    з самої топології, а не з vtkPolyDataNormals: той на збірці з
    non-manifold ребрами вигадує вивернуті ділянки там, де їх немає.
    """
    faces = _tri_faces(mesh)
    if faces is None:
        return 0
    n = int(mesh.n_points)
    e = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    directed = e[:, 0].astype(np.int64) * n + e[:, 1]
    und = np.sort(e, axis=1)
    key = und[:, 0].astype(np.int64) * n + und[:, 1]

    order = np.argsort(key, kind="stable")
    key_s, dir_s = key[order], directed[order]
    uniq, start, count = np.unique(key_s, return_index=True, return_counts=True)
    two = start[count == 2]
    if two.size == 0:
        return 0
    return int(np.count_nonzero(dir_s[two] == dir_s[two + 1]))


def _signed_volume(mesh: pv.PolyData) -> float:
    """Обʼєм через теорему про дивергенцію: V = 1/6 * sum(v0 . (v1 x v2)).

    Знак каже, чи нормалі дивляться назовні. Для незамкненої поверхні число
    не має фізичного сенсу, але знак усе одно корисний.
    """
    faces = mesh.faces.reshape(-1, 4)
    if faces.size == 0 or not np.all(faces[:, 0] == 3):
        return 0.0
    p = mesh.points
    v0, v1, v2 = p[faces[:, 1]], p[faces[:, 2]], p[faces[:, 3]]
    return float(np.sum(np.einsum("ij,ij->i", v0, np.cross(v1, v2))) / 6.0)


def _hole_sizes(mesh: pv.PolyData, limit: int = 400) -> list[float]:
    """Розмір кожної дірки — діагональ габариту її контуру, за спаданням.

    Саме це число вирішує долю сітки: дірку, дрібнішу за комірку в тій зоні,
    snappyHexMesh затягує сам (усі комірки навколо неї він однаково зрізає),
    а більша стає наскрізним каналом усередину тіла.
    """
    edges = mesh.extract_feature_edges(
        boundary_edges=True, feature_edges=False,
        manifold_edges=False, non_manifold_edges=False)
    if edges.n_cells == 0:
        return []

    sizes = []
    for p in _region_points(edges, limit) or []:
        if len(p) < 2:
            continue
        sizes.append(float(np.linalg.norm(p.max(axis=0) - p.min(axis=0))))
    sizes.sort(reverse=True)
    return sizes


def projected_areas(mesh: pv.PolyData,
                    target_voxels: int = 6_000_000) -> tuple[float, float, float]:
    """Площі силуету тіла вздовж X, Y, Z — рахуємо растром, а не формулою.

    ЧОМУ НЕ ФОРМУЛОЮ. Раніше тут стояла половина суми |n_i| * area по всіх
    гранях. Це точно лише для ВИПУКЛОГО тіла, де кожен промінь перетинає
    поверхню двічі. Літак невипуклий: промінь уздовж X проходить і крізь
    фюзеляж, і крізь корінь крила — чотири перетини, і формула лічить два
    силуети замість одного. Виміряно на хресті з двох циліндрів, де відповідь
    відома точно (0.04 м²): формула дає 0.0478 (+20%), растр — 0.0398
    (-0.6%). На реальній збірці з внутрішніми стінками формула завищувала
    мідель удвічі.

    Ціна помилки пряма: Aref входить у знаменник Cd і Cl.

    РАСТРОМ. Воксельна оболонка тіла, спроєктована на кожну площину. Растр
    «товщає» на пів вокселя по контуру, тому віднімаємо половину межової
    смуги — на аналітичних тілах це дає похибку в межах 1%.
    """
    if mesh.n_cells == 0 or int(mesh.GetNumberOfPolys()) == 0:
        return (0.0, 0.0, 0.0)
    b = np.array(mesh.bounds).reshape(3, 2)
    size = b[:, 1] - b[:, 0]
    span = float(size.max())
    if span <= 0:
        return (0.0, 0.0, 0.0)

    # Плоске тіло має нульовий габарит по одній осі — для вибору вокселя
    # підставляємо туди тонкий, але не нульовий розмір.
    thick = np.maximum(size, span / 1000.0)
    voxel = float((float(np.prod(thick)) / target_voxels) ** (1.0 / 3.0))
    # Не грубіше за 1/40 найбільшої сторони (інакше контур силуету з
    # кількох пікселів) і не дрібніше за 1/600 (інакше сітка не влізе).
    voxel = min(max(voxel, span / 600.0), span / 40.0)

    origin, dims = _voxel_grid(mesh, voxel, pad=2)
    rs = _rust_mesh()
    arrays = _pure_triangles(mesh) if rs is not None else None
    if arrays is not None:
        # Той самий растр, що й vtkVoxelModeller (точка сітки на оболонці,
        # якщо до трикутника ≤ max_dist), але одразу три проєкції.
        span_grid = max((d - 1) * voxel for d in dims)
        max_dist = min(1.0, voxel / span_grid) * span_grid if span_grid > 0 else voxel
        flats = [np.asarray(f) > 0 for f in rs.shell_projections(
            arrays[0], arrays[1], tuple(float(v) for v in origin),
            tuple(int(v) for v in dims), float(voxel), float(max_dist))]
    else:
        shell = _shell_voxels(mesh, origin, dims, voxel)
        flats = [shell.any(axis=axis) for axis in range(3)]
    out = []
    for axis in range(3):
        flat = flats[axis]
        if not flat.any():
            out.append(0.0)
            continue
        inner = flat.copy()
        for plane_axis in range(2):
            for shift in (1, -1):
                inner &= np.roll(flat, shift, axis=plane_axis)
        border = float(flat.sum() - inner.sum())
        out.append(max(0.0, (float(flat.sum()) - 0.5 * border)) * voxel * voxel)
    return (out[0], out[1], out[2])


def open_edges_polydata(mesh: pv.PolyData) -> pv.PolyData:
    """Самі відкриті ребра — щоб підсвітити їх у вʼюпорті.

    Показати місце проблеми важливіше, ніж назвати її кількість: дірки в
    імпортованій моделі майже завжди сидять у кількох конкретних вузлах
    (пази, стики деталей, зрізи), і побачивши їх, зрозуміло, що правити.
    """
    return mesh.extract_feature_edges(
        boundary_edges=True, feature_edges=False,
        manifold_edges=False, non_manifold_edges=False)


def hole_report(mesh: pv.PolyData, limit: int = 12) -> list[tuple[float, tuple]]:
    """[(розмір, центр)] для найбільших дірок, за спаданням розміру."""
    edges = open_edges_polydata(mesh)
    if edges.n_cells == 0:
        return []

    out = []
    for p in _region_points(edges) or []:
        if len(p) < 2:
            continue
        size = float(np.linalg.norm(p.max(axis=0) - p.min(axis=0)))
        out.append((size, tuple(float(v) for v in p.mean(axis=0))))
    out.sort(key=lambda t: -t[0])
    return out[:limit]


def problem_spots(mesh: pv.PolyData, diagnosis, limit: int = 10) -> list[dict]:
    """Де саме сидять проблеми: [{вид, розмір, центр}], найбільші спершу.

    Підсвітка показує проблеми на моделі, але знайти рожеву цятку в 775
    тисячах трикутників очима — окрема робота. Це список, яким можна
    ходити камерою.
    """
    spots: list[dict] = []
    for size, centre in hole_report(mesh, limit=limit):
        spots.append({"kind": "holes", "size": size, "centre": centre,
                      "what": "hole"})

    mask = getattr(diagnosis, "inverted_cells", None)
    if mask is not None:
        inverted = inverted_polydata(mesh, mask)
        if inverted is not None and inverted.n_cells:
            groups = []
            for pick in _region_points(inverted, 200) or []:
                if len(pick) < 3:
                    continue
                span = float(np.linalg.norm(pick.max(axis=0) - pick.min(axis=0)))
                groups.append((span, tuple(float(v) for v in pick.mean(axis=0))))
            groups.sort(key=lambda g: -g[0])
            for span, centre in groups[:limit]:
                spots.append({"kind": "flip", "size": span, "centre": centre,
                              "what": "flipped part"})

    degen = degenerate_points(mesh)
    if degen is not None and degen.n_points:
        step = max(1, degen.n_points // limit)
        for point in np.asarray(degen.points)[::step][:limit]:
            spots.append({"kind": "degen", "size": 0.0,
                          "centre": tuple(float(v) for v in point),
                          "what": "degenerate triangle"})

    spots.sort(key=lambda spot: -spot["size"])
    return spots


def _edge_stats(mesh: pv.PolyData) -> tuple[float, float, float, float]:
    """(найкоротше, найдовше, медіана всіх, медіана найкоротших у трикутнику).

    Останнє окремо, бо трикутники бувають витягнуті. У лофтованого крила
    кожен трикутник має одне коротке ребро впоперек хорди й два довгих
    уздовж розмаху: медіана всіх ребер тоді описує крок між перерізами, а
    медіана коротких — те, наскільки дрібно передано сам профіль.
    """
    faces = mesh.faces.reshape(-1, 4)
    if faces.size == 0:
        return (0.0, 0.0, 0.0, 0.0)
    p = mesh.points
    a, b, c = p[faces[:, 1]], p[faces[:, 2]], p[faces[:, 3]]
    sides = np.column_stack([
        np.linalg.norm(b - a, axis=1),
        np.linalg.norm(c - b, axis=1),
        np.linalg.norm(a - c, axis=1),
    ])
    lens = sides.reshape(-1)
    lens = lens[lens > 0]
    if lens.size == 0:
        return (0.0, 0.0, 0.0, 0.0)
    shortest = sides.min(axis=1)
    shortest = shortest[shortest > 0]
    short = float(np.median(shortest)) if shortest.size else float(lens.min())
    return (float(lens.min()), float(lens.max()), float(np.median(lens)),
            short)


def _rust_mesh():
    """foamrs, якщо вміє перевірку геометрії (старий .pyd може не вміти)."""
    try:
        import fs_fast
    except ImportError:
        return None
    rs = getattr(fs_fast, "_rs", None)
    return rs if rs is not None and hasattr(rs, "mesh_check") else None


def _pure_triangles(mesh: pv.PolyData):
    """(points float64 (N,3), tris int64 (M,3)), якщо сітка — самі
    трикутники; інакше None (тоді лишається шлях VTK/numpy)."""
    if mesh.n_cells == 0 or int(mesh.GetNumberOfPolys()) != int(mesh.n_cells):
        return None
    faces = _tri_faces(mesh)
    if faces is None or faces.shape[0] != mesh.n_cells:
        return None
    return (np.ascontiguousarray(mesh.points, dtype=np.float64),
            np.ascontiguousarray(faces, dtype=np.int64))


def diagnose(mesh: pv.PolyData) -> Diagnosis:
    """Повна перевірка геометрії.

    На Rust (foamrs.mesh_check), якщо він є: ті самі числа за один прохід
    (250 тис. трикутників — десятки мілісекунд замість ~1.4 с, а
    відкриття проєкту перевіряло сцену кілька разів поспіль).
    """
    rs = _rust_mesh()
    arrays = _pure_triangles(mesh) if rs is not None else None
    if arrays is not None:
        return _diagnose_rust(mesh, rs, *arrays)
    d = Diagnosis()
    d.n_triangles = int(mesh.n_cells)
    d.n_points = int(mesh.n_points)
    d.bounds = tuple(float(x) for x in mesh.bounds)

    d.open_edges = _count_edges(mesh, boundary_edges=True)
    d.non_manifold_edges = _count_edges(mesh, non_manifold_edges=True)
    n_regions, regions = _region_ids(mesh)
    d.parts = n_regions

    sized = mesh.compute_cell_sizes(length=False, area=True, volume=False)
    areas = np.asarray(sized.cell_data["Area"])
    d.area = float(areas.sum())
    # Поріг вироджености: площа менша за 1e-10 від середньої.
    if areas.size:
        d.degenerate = int(np.count_nonzero(areas < max(areas.mean() * 1e-10, 1e-24)))


    vols = _part_volumes(mesh, regions, n_regions)
    bad = np.where(vols < 0)[0]
    d.inverted_parts = int(bad.size)
    d.inverted_cells = np.isin(regions, bad) if bad.size else None
    d.flipped_faces = int(d.inverted_cells.sum()) if bad.size else 0
    d.bad_winding = _bad_winding_edges(mesh)
    d.projected = projected_areas(mesh)
    d.signed_volume = _signed_volume(mesh)
    d.volume = abs(d.signed_volume)
    d.min_edge, d.max_edge, d.median_edge, d.short_edge = _edge_stats(mesh)
    if d.open_edges:
        d.hole_sizes = _hole_sizes(mesh)

    _add_notes(d)
    return d


def _diagnose_rust(mesh, rs, pts, tris) -> Diagnosis:
    d = Diagnosis()
    d.n_triangles = int(mesh.n_cells)
    d.n_points = int(mesh.n_points)
    d.bounds = tuple(float(x) for x in mesh.bounds)
    r = rs.mesh_check(pts, tris)
    d.open_edges = int(r["open_edges"])
    d.non_manifold_edges = int(r["non_manifold_edges"])
    d.parts = int(r["n_regions"])
    regions = np.asarray(r["regions"])
    d.area = float(r["area"])
    d.degenerate = int(r["degenerate"])
    vols = np.asarray(r["part_volumes"])
    bad = np.where(vols < 0)[0]
    d.inverted_parts = int(bad.size)
    d.inverted_cells = np.isin(regions, bad) if bad.size else None
    d.flipped_faces = int(d.inverted_cells.sum()) if bad.size else 0
    d.bad_winding = int(r["bad_winding"])
    d.projected = projected_areas(mesh)
    d.signed_volume = float(r["signed_volume"])
    d.volume = abs(d.signed_volume)
    d.min_edge, d.max_edge, d.median_edge, d.short_edge = (
        float(x) for x in r["edge_stats"])
    if d.open_edges:
        d.hole_sizes = [float(x) for x in r["hole_sizes"]]
    _add_notes(d)
    return d


def _add_notes(d: Diagnosis) -> None:
    n = d.notes
    if d.n_triangles == 0:
        n.append(("bad", tr("The surface is empty.")))
        return

    if d.open_edges == 0:
        n.append(("ok", tr("The surface is closed — there will be no leaks.")))
    else:
        from fs_physics import fmt_len
        big = fmt_len(d.largest_hole) if d.hole_sizes else "?"
        n.append((
            "bad",
            tr("{open_edges} open edges, {n_hole_sizes} holes. The "
               "largest — {big}. Compare it with the cell size on the "
               "surface: snappy closes holes smaller than a cell by "
               "itself, larger ones become a channel into the body.", open_edges=d.open_edges, n_hole_sizes=len(d.hole_sizes), big=big),
        ))

    if d.non_manifold_edges:
        n.append((
            "warn",
            tr("{non_manifold_edges} non-manifold edges (an edge shared "
               "by >2 triangles) — a typical sign of an assembly of "
               "parts not joined by a boolean union. Castellation "
               "tolerates it, but surfaceFeatureExtract will give junk "
               "feature edges: raise includedAngle to 170.", non_manifold_edges=d.non_manifold_edges),
        ))

    if d.degenerate:
        n.append(("warn", tr("{degenerate} degenerate triangles with zero area.", degenerate=d.degenerate)))

    if d.parts > 1:
        # Для snappyHexMesh це не вада: набір перетинних замкнених оболонок
        # він обробляє так само, як одне тіло — усе вирішує відсутність
        # наскрізних дірок. Погано лише тоді, коли серед частин є сміття.
        n.append((
            "ok" if d.closed else "warn",
            tr("{parts} unconnected parts. For snappy this is normal if "
               "it is an assembly of intersecting parts; just check that "
               "there is no random junk among them.", parts=d.parts),
        ))

    if d.closed and not d.normals_outward:
        n.append((
            "warn",
            tr("The normals point inward. For castellation this mostly does "
            "not matter, but it is cheap to fix."),
        ))

    # Напрямок потоку з самої геометрії надійно не визначити: у крила
    # найтонший переріз — уздовж РОЗМАХУ, а не потоку, тому правило «потік
    # туди, де силует найменший» на крилі дає хибне спрацювання. Надійне
    # лише одне спостереження: у літака найбільший габарит — це розмах.
    sx, sy, sz = d.size
    if any(d.projected) and sx > max(sy, sz) * 1.05:
        n.append((
            "warn",
            tr("The largest dimension ({sx:.4g} m) lies along X — where "
               "the flow goes in this program. For an aircraft this is "
               "usually the span, and it must be along Y: check the "
               "orientation with the “↻ Z 90°” button. For a car, hull "
               "or rocket all is fine — there the longest axis is the "
               "direction of motion.", sx=sx),
        ))

    if d.bad_winding:
        n.append((
            "bad",
            tr("{bad_winding} edges where neighboring triangles are "
               "wound the same way — the surface winding is "
               "inconsistent. This breaks both “inside/outside” checks "
               "in snappy: refinementRegions with mode inside and the "
               "closedness check. Click “Repair” (meshfix method).", bad_winding=d.bad_winding),
        ))

    if d.inverted_parts:
        n.append((
            "warn" if d.flipped_fraction < 0.25 else "bad",
            tr("{inverted_parts} of {parts} parts are turned inside out "
               "({flipped_faces} faces): they are wound correctly within "
               "themselves, but their normals point inward. You cannot "
               "see this in Blender — its viewport lights both sides. "
               "“Repair” (meshfix method) turns each part outward.", inverted_parts=d.inverted_parts, parts=d.parts, flipped_faces=d.flipped_faces),
        ))

    if d.max_edge > 0 and d.min_edge > 0:
        ratio = d.max_edge / d.min_edge
        if ratio > 1e4:
            n.append((
                "warn",
                tr("Edge length spread {ratio:.0f}x — the mesh has both "
                   "slivers and huge faces.", ratio=ratio),
            ))


def reoriented(d: Diagnosis, axis: str, degrees: float,
               bounds) -> Diagnosis | None:
    """Діагноз після повороту на кратний 90° кут — без перерахунку.

    Поворот не міняє НІЧОГО, крім габариту й того, який силует куди
    дивиться: трикутники, ребра, частини, площа, обʼєм, дірки лишаються ті
    самі, а силуети просто переставляються місцями. Перерахунок на моделі в
    775 тис. трикутників коштує близько чотирьох секунд — і всі чотири
    витрачаються на те, щоб отримати ті самі числа.

    Повертає None, якщо кут не кратний 90°: тоді осі не переставляються, і
    чесніше порахувати заново.
    """
    import copy

    steps = int(round(degrees / 90.0))
    if abs(degrees - steps * 90.0) > 1e-9:
        return None
    out = copy.copy(d)
    out.notes = []
    out.bounds = tuple(float(v) for v in bounds)
    order = {"x": (1, 2), "y": (2, 0), "z": (0, 1)}.get(axis)
    if order is None:
        return None
    projected = list(d.projected)
    first, second = order
    for _ in range(steps % 4):
        projected[first], projected[second] = projected[second], projected[first]
    out.projected = (projected[0], projected[1], projected[2])
    _add_notes(out)
    return out


def diagnosis_rows(d: Diagnosis) -> list[tuple[str, str, str]]:
    """Рядки для таблиці інтерфейсу: (назва, значення, рівень)."""
    from fs_physics import fmt_len

    def lvl(cond_bad, cond_warn=False):
        return "bad" if cond_bad else ("warn" if cond_warn else "ok")

    sx, sy, sz = d.size
    rows = [
        (tr("Triangles"), fmt.count(d.n_triangles), "ok"),
        (tr("Vertices"), fmt.count(d.n_points), "ok"),
        (tr("Closed"), tr("yes") if d.closed else tr("NO"), lvl(not d.closed)),
        (tr("Open edges"), str(d.open_edges), lvl(d.open_edges > 0)),
        (tr("Holes / largest"),
         f"{len(d.hole_sizes)} / {fmt_len(d.largest_hole)}" if d.hole_sizes else "—",
         lvl(bool(d.hole_sizes))),
        (tr("Non-manifold edges"), str(d.non_manifold_edges),
         lvl(False, d.non_manifold_edges > 0)),
        (tr("Degenerate", ctx="count"), str(d.degenerate), lvl(False, d.degenerate > 0)),
        (tr("Parts"), str(d.parts), lvl(False, d.parts > 1)),
        (tr("Normals outward"), tr("yes") if d.normals_outward else tr("no"),
         lvl(False, not d.normals_outward)),
        (tr("Flipped parts"),
         tr("{inverted_parts} / {parts} ({flipped_faces} faces)", inverted_parts=d.inverted_parts, parts=d.parts, flipped_faces=d.flipped_faces)
         if d.inverted_parts else "0",
         lvl(d.flipped_fraction > 0.25, d.inverted_parts > 0)),
        (tr("Inconsistent edges"), str(d.bad_winding), lvl(d.bad_winding > 0)),
        (tr("Surface area"), tr("{area:.6g} m²", area=d.area), "ok"),
        (tr("Front silhouette (frontal area)"), tr("{frontal:.5g} m²", frontal=d.frontal),
         lvl(False, d.streamwise_axis != 0)),
        (tr("Top silhouette (planform)"), tr("{planform:.5g} m²", planform=d.planform), "ok"),
        (tr("Volume"), tr("{volume:.6g} m³", volume=d.volume) if d.closed else tr("— (not closed)"), "ok"),
        (tr("Overall size"), tr("{sx:.4g} × {sy:.4g} × {sz:.4g} m", sx=sx, sy=sy, sz=sz), "ok"),
        (tr("Edge min / median / max"),
         f"{fmt_len(d.min_edge)} / {fmt_len(d.median_edge)} / {fmt_len(d.max_edge)}",
         "ok"),
    ]
    return rows


# --------------------------------------------------------------------------
# Перетворення та ремонт
# --------------------------------------------------------------------------

def apply_scale(mesh: pv.PolyData, factor: float) -> pv.PolyData:
    if factor == 1.0:
        return mesh
    out = mesh.copy()
    out.points = out.points * factor
    return out


def rotate(mesh: pv.PolyData, axis: str, degrees: float,
           pivot=None) -> pv.PolyData:
    """Повертає навколо ЦЕНТРА моделі, а не навколо початку координат.

    Типово pyvista крутить навколо (0,0,0). Модель, чий центр зміщений від
    початку координат — а так буває майже завжди, — при цьому не просто
    повертається, а відʼїжджає вбік на відстань свого зміщення. Виглядає це
    так, ніби кнопка робить щось зовсім не те.

    `pivot` потрібен, коли в сцені кілька моделей: кожну з них треба крутити
    навколо СПІЛЬНОГО центра збірки, інакше крило поїде окремо від фюзеляжу.
    """
    out = mesh.copy()
    fn = {"x": out.rotate_x, "y": out.rotate_y, "z": out.rotate_z}[axis]
    return fn(degrees, point=out.center if pivot is None else pivot,
              inplace=False)


def translate(mesh: pv.PolyData, shift) -> pv.PolyData:
    out = mesh.copy()
    out.points = out.points + np.asarray(shift, dtype=float)
    return out


def hinge_pose(mesh: pv.PolyData, pivot, axis, degrees: float,
               offset=(0.0, 0.0, 0.0)) -> pv.PolyData:
    """Відхилити деталь навколо ЗАВІСИ — прямої, а не точки.

    НАВІЩО ОКРЕМА ФУНКЦІЯ, коли є `rotate`. Бо `rotate` крутить навколо
    центра моделі, і для елерона це неправильно: центр елерона лежить
    посеред його хорди, а завіса — на передній кромці. Поворот навколо
    центра піднімає передню кромку над крилом і топить задню в обшивці,
    тобто деталь СПОВЗАЄ з гнізда. Виглядає це як дірка в крилі, а
    snappyHexMesh на такій геометрії або протікає, або ліпить сходинку.

    Завіса — це пряма: точка `pivot` і напрямок `axis`. Усі точки на цій
    прямій лишаються на місці за означенням, тож деталь нікуди не їде.

    `offset` — поправка на залишковий зсув. Потрібна тоді, коли завіса в
    моделі не збігається з тією, що в кресленні: CAD часто дає деталь,
    зняту з вузла, і її треба дослати на місце. Поправка застосовується
    ПІСЛЯ повороту, бо вона виправляє результат, а не вихідне положення.

    Функція НЕ змінює вхідну модель: пульт рухає поверхні туди-сюди, і
    накопичувати повороти в самому mesh означало б, що нейтраль з часом
    поїде. Тому щоразу від базової геометрії.
    """
    v = np.asarray(axis, dtype=float)
    n = float(np.linalg.norm(v))
    if n < 1e-12:
        # Вісь нульової довжини — це не поворот, а мовчазна помилка.
        # Повертаємо копію зі зсувом: хоч поправка спрацює.
        return translate(mesh, offset)
    out = mesh.copy()
    if abs(degrees) > 1e-12:
        out = out.rotate_vector(tuple(v / n), float(degrees),
                                point=tuple(float(p) for p in pivot),
                                inplace=False)
    if any(abs(float(o)) > 1e-12 for o in offset):
        out = translate(out, offset)
    return out


def quick_facts(mesh: pv.PolyData) -> tuple[int, int]:
    """(відкритих ребер, неузгоджених ребер) самим numpy, без VTK.

    Для позначки біля кожної моделі в списку: повна діагностика збірки з
    800 тис. трикутників — це секунди, а тут потрібні лише два числа.
    """
    faces = _tri_faces(mesh)
    if faces is None:
        return (0, 0)
    n = int(mesh.n_points)
    e = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    directed = e[:, 0].astype(np.int64) * n + e[:, 1]
    und = np.sort(e, axis=1)
    key = und[:, 0].astype(np.int64) * n + und[:, 1]
    order = np.argsort(key, kind="stable")
    key_s, dir_s = key[order], directed[order]
    _, start, count = np.unique(key_s, return_index=True, return_counts=True)
    two = start[count == 2]
    bad = int(np.count_nonzero(dir_s[two] == dir_s[two + 1])) if two.size else 0
    return (int((count == 1).sum()), bad)


def weld(mesh: pv.PolyData, tolerance_m: float,
         unit_factor: float = 1.0) -> pv.PolyData:
    """Зварює вершини, ближчі за `tolerance_m` МЕТРІВ, і прибирає вироджені.

    Аналог «Merge by Distance» у Blender. На імпортованих збірках це часто
    єдине, що реально зменшує кількість відкритих ребер: щілини між деталями
    здебільшого виникають від того, що збіжні вершини не зшиті.

    ПАСТКА З ОДИНИЦЯМИ, мовчазна: `mesh` тут — вихідна геометрія у власних
    одиницях файлу (зазвичай міліметри), а допуск задається в метрах.
    `unit_factor` — множник «одиниці файлу -> метри» (0.001 для мм). Без
    ділення на нього допуск 1 мм стає 0.000001 мм і функція нічого не робить.

    `absolute=True` передається явно. У PyVista це й так типове значення, але
    покладатися на нього не варто: різниця між абсолютним допуском і часткою
    діагоналі габариту тут — три порядки, і помилка не дає жодного сигналу,
    просто тихо не зшиває нічого.
    """
    factor = unit_factor if unit_factor > 0 else 1.0
    tol_in_mesh_units = tolerance_m / factor
    out = mesh.clean(tolerance=tol_in_mesh_units, absolute=True,
                     point_merging=True, lines_to_points=True,
                     polys_to_lines=True)
    return _require_cells(
        drop_stray_cells(out.triangulate()), tr("Weld vertices"),
        tr("Tolerance {v:.3g} mm merged the whole model into one point — "
           "use a smaller one.", v=tolerance_m * 1000))


def fill_holes(mesh: pv.PolyData, max_size: float) -> pv.PolyData:
    """vtkFillHolesFilter: закриває дірки плоскою «латкою».

    ВАЖЛИВЕ ОБМЕЖЕННЯ: фільтр працює лише з дірками, обведеними одним
    замкненим контуром граничних ребер на manifold-поверхні. На збірці з
    перетинних деталей (багато non-manifold ребер) більшість «дірок» — це не
    контури, а лінії взаємного врізання оболонок, і фільтр їх не бере.
    Тому на брудних моделях результат буває майже нульовим; це не збій, а межа
    методу. Такі файли лагодять у MeshLab, Blender або обгортанням (shrink
    wrap) у CAD.
    """
    out = mesh.fill_holes(max_size)
    out = drop_stray_cells(out.clean(tolerance=1e-9).triangulate())
    return orient_normals(_require_cells(out, tr("Close holes")))


def orient_parts_outward(mesh: pv.PolyData) -> pv.PolyData:
    """Розвертає назовні КОЖНУ незвʼязану частину окремо.

    Глобального знака обʼєму для збірки не досить: у моделі з 24 деталей
    17 можуть дивитися всередину, а сумарний обʼєм лишатиметься додатним.
    vtkPolyDataNormals з auto_orient_normals теж не рятує — він вимагає
    замкненої manifold-поверхні, а тут її немає.
    """
    faces = _tri_faces(mesh)
    if faces is None:
        return mesh.copy()
    mask = inverted_mask(mesh)
    if not mask.any():
        return mesh.copy()
    flipped = faces.copy()
    flipped[mask] = flipped[mask][:, [0, 2, 1]]
    out = mesh.copy()
    out.faces = np.hstack(
        [np.full((len(flipped), 1), 3, dtype=flipped.dtype), flipped]).ravel()
    # Масиви нормалей після перевертання намотки застаріли: лишити їх —
    # означає отримати поверхню, де нормаль і обхід дивляться в різні боки,
    # а це саме те, від чого тут рятуємось.
    out.cell_data.pop("Normals", None)
    out.point_data.pop("Normals", None)
    return out


def orient_normals(mesh: pv.PolyData) -> pv.PolyData:
    """Узгоджує намотку в межах кожної частини й розвертає частини назовні.

    ДВА ПРОХОДИ, і обидва потрібні. Перший узгоджує сусідні грані між собою,
    другий вирішує, куди дивиться частина в цілому — це видно тільки зі знака
    її обʼєму.

    `non_manifold_traversal=False` — не косметика. З увімкненим обходом (а це
    типове значення в pyvista) vtkPolyDataNormals переносить орієнтацію через
    ребра, які ділять три й більше трикутників, і на збірці з перетинних
    деталей вивертає цілі ділянки: на реальній моделі — 14.8% граней із 267
    тис., на важчій — 39% із 775 тис. Саме це малювало «рвану» модель і
    писало в діагностику неіснуючі вивернуті грані.
    """
    before = _bad_winding_edges(mesh)
    out = mesh
    if before:
        out = mesh.copy()
        out.compute_normals(consistent_normals=True, auto_orient_normals=False,
                            non_manifold_traversal=False, inplace=True)
        # На збірці з 57 тис. non-manifold ребер цей прохід уміє наробити
        # неузгоджености замість того, щоб її прибрати: він не переходить
        # через такі ребра, тому вивертає один бік і лишає інший. Міряємо
        # результат і відкочуємо, якщо стало гірше — тихо зіпсувати намотку
        # кнопкою «Виправити» гірше, ніж не зробити нічого.
        if _bad_winding_edges(out) >= before:
            out = mesh
    return orient_parts_outward(out)


def display_mesh(mesh: pv.PolyData, feature_angle: float = 45.0) -> pv.PolyData:
    """Копія поверхні з нормалями, придатними для показу на екрані.

    Робить рівно те, чого НЕ можна довіряти типовому `smooth_shading=True`:
    той щоразу перераховує нормалі з `non_manifold_traversal=True` і на
    брудній збірці вивертає кожну сьому грань — вони темніють, і модель
    виглядає рваною, хоч у файлі все гаразд.

    `split_vertices` лишає різкими справжні кромки: без нього нормалі
    усереднюються через гравіювання панелей, і поверхня пливе.
    """
    if int(mesh.GetNumberOfPolys()) == 0:
        # Нема з чого рахувати нормалі: віддаємо як є, щоб вʼюпорт показав
        # хоч що-небудь, а не впав із трасуванням.
        return mesh.copy()
    out = orient_parts_outward(mesh)
    out.compute_normals(cell_normals=True, point_normals=True,
                        consistent_normals=False, auto_orient_normals=False,
                        non_manifold_traversal=False,
                        split_vertices=True, feature_angle=feature_angle,
                        inplace=True)
    return out


def surface_deviation(mesh: pv.PolyData, reference: pv.PolyData,
                      sample: int = 50_000) -> tuple[float, float, float]:
    """(медіана, 90-й відсоток, максимум) відстані від `mesh` до `reference`.

    Питання «чи не зʼїло обгортання щось важливе» словами не вирішується:
    обʼєм може зійтися при поїханій формі, а габарит нічого не каже про
    середину. Тому міряємо саму відстань між поверхнями.

    На великій моделі беремо випадкову вибірку вершин: для медіани й
    відсотка 50 тисяч точок дають те саме число, що й мільйон, а часу
    забирають у двадцять разів менше.
    """
    if mesh.n_points == 0 or reference.n_cells == 0:
        return (0.0, 0.0, 0.0)
    pts = mesh.points
    if pts.shape[0] > sample:
        rng = np.random.default_rng(0)     # відтворюваність важливіша за випадковість
        pts = pts[rng.choice(pts.shape[0], sample, replace=False)]
    probe = pv.PolyData(pts)
    dist = np.abs(np.asarray(
        probe.compute_implicit_distance(reference)["implicit_distance"]))
    return (float(np.median(dist)), float(np.percentile(dist, 90)),
            float(dist.max()))


def non_manifold_polydata(mesh: pv.PolyData) -> pv.PolyData:
    """Non-manifold ребра — щоб показати, де саме деталі врізані одна в одну."""
    return mesh.extract_feature_edges(
        boundary_edges=False, feature_edges=False,
        manifold_edges=False, non_manifold_edges=True)


def inverted_polydata(mesh: pv.PolyData,
                      mask: np.ndarray | None = None) -> pv.PolyData | None:
    """Грані вивернутих частин окремою поверхнею — для підсвітки."""
    if mask is None:
        mask = inverted_mask(mesh)
    mask = np.asarray(mask, dtype=bool)
    if mask.size != mesh.n_cells or not mask.any():
        return None
    return mesh.extract_cells(np.where(mask)[0]).extract_surface(
        algorithm="dataset_surface")


def degenerate_points(mesh: pv.PolyData) -> pv.PolyData | None:
    """Центри вироджених трикутників. Самих трикутників не видно — площа нуль,
    тому показуємо точки: інакше «6 вироджених» у таблиці ні про що не каже."""
    faces = _tri_faces(mesh)
    if faces is None:
        return None
    p = mesh.points
    v0, v1, v2 = p[faces[:, 0]], p[faces[:, 1]], p[faces[:, 2]]
    area = np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1) / 2.0
    if not area.size:
        return None
    bad = area < max(area.mean() * 1e-10, 1e-24)
    if not bad.any():
        return None
    centres = (v0[bad] + v1[bad] + v2[bad]) / 3.0
    return pv.PolyData(centres)


def decimate(mesh: pv.PolyData, target_triangles: int) -> pv.PolyData:
    """Проріджування зі збереженням топології (vtkQuadricDecimation)."""
    if mesh.n_cells <= target_triangles or mesh.n_cells == 0:
        return mesh
    ratio = 1.0 - target_triangles / mesh.n_cells
    return _require_cells(
        drop_stray_cells(
            mesh.decimate(ratio, volume_preservation=True).triangulate()),
        tr("Decimate"),
        tr("Try to keep more triangles."))


def subdivide(mesh: pv.PolyData, levels: int = 1) -> pv.PolyData:
    """Згущення тесселяції — коли STL надто грубий для потрібного розміру комірки.

    ПАСТКА: vtkLinearSubdivisionFilter не бере non-manifold сітку. Він не
    кидає винятку, а пише зауваження у вікно виводу VTK і повертає НУЛЬ
    трикутників — модель зникала молча, і повернути її можна було тільки
    кнопкою «Скинути». Тому перевіряємо вхід заздалегідь, щоб сказати
    людині, що саме не так, і перевіряємо вихід — на випадок інших причин.
    """
    bad = _count_edges(mesh, non_manifold_edges=True)
    if bad:
        raise ValueError(
            tr("Cannot subdivide: the mesh has {bad} non-manifold edges, "
               "and vtkLinearSubdivisionFilter does not accept that — it "
               "returns an empty surface. First use “Repair” (meshfix or "
               "alpha wrap).", bad=bad))
    return _require_cells(
        drop_stray_cells(
            mesh.subdivide(levels, subfilter="linear").triangulate()),
        "Subdivide")


def repair_chain(mesh: pv.PolyData,
                 tolerance_m: float | None = None) -> tuple[pv.PolyData, float]:
    """Повний ремонт одним натиском — у тому порядку, у якому це роблять
    слайсери (admesh у PrusaSlicer і Orca).

    Порядок не довільний:

    1. **Зшити вершини.** Доки деталі не зшиті, «дірка» і «щілина між
       деталями» для будь-якого наступного кроку — те саме.
    2. **Узгодити намотку й розвернути частини назовні.** Заливати дірки з
       неузгодженими нормалями немає сенсу: латка не знатиме, з якого боку
       вона зовні.
    3. **Закрити дірки** й розвернути ще раз — латки додають нові грані.

    Повертає (поверхня, застосований допуск зварювання в метрах). Допуск
    типово беремо від самої сітки: чверть медіанного ребра, але не більше
    0.1 мм — більше вже зриває тонкі кромки.
    """
    if mesh.n_cells == 0:
        return mesh, 0.0
    if tolerance_m is None:
        _, _, median, _ = _edge_stats(mesh)
        tolerance_m = min(1e-4, median * 0.25) if median > 0 else 1e-4

    out = weld(mesh, tolerance_m, unit_factor=1.0)
    out = orient_normals(out)
    if _count_edges(out, boundary_edges=True):
        span = max(out.bounds[1] - out.bounds[0], 1e-9)
        # fill_holes сам закінчує розворотом нормалей
        out = fill_holes(out, span * 0.5)
    # Викидати сливери, якими лягають латки, НЕ можна, хоч і кортить: на
    # реальній моделі 121 вироджений трикутник тримає поверхню, і без них
    # 100 відкритих ребер перетворюються на 323. Вони нульової площі —
    # snappy на них не зважає, а от дірка замість них була б справжня.
    return out, tolerance_m


def voxel_limits(mesh: pv.PolyData,
                 limit: int = 60_000_000) -> tuple[float, float]:
    """(найдрібніший воксель, який влізе в памʼять; розумний типовий).

    Обидва числа — у тих самих одиницях, що й сітка.
    """
    b = np.array(mesh.bounds).reshape(3, 2)
    size = np.maximum(b[:, 1] - b[:, 0], 1e-12)
    # N = prod(size/v + 9) ~ prod(size)/v^3 при дрібному вокселі
    smallest = float((float(np.prod(size)) / limit) ** (1.0 / 3.0))
    span = float(size.max())
    return (smallest, max(span / 400.0, smallest))


def _voxel_grid(mesh: pv.PolyData, voxel: float,
                pad: int = 4) -> tuple[np.ndarray, tuple[int, int, int]]:
    """Початок і розміри сітки вокселів. Запас по краях — щоб тіло не
    торкалося межі: заливка ззовні мусить мати звідки початися."""
    b = np.array(mesh.bounds).reshape(3, 2)
    size = b[:, 1] - b[:, 0]
    margin = voxel * pad
    dims = np.ceil((size + 2 * margin) / voxel).astype(int) + 1
    return b[:, 0] - margin, tuple(int(v) for v in dims)


def _blank(origin, dims, voxel: float, fill: int = 0) -> vtk.vtkImageData:
    img = vtk.vtkImageData()
    img.SetDimensions(*dims)
    img.SetSpacing(voxel, voxel, voxel)
    img.SetOrigin(*[float(v) for v in origin])
    img.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
    img.GetPointData().GetScalars().Fill(fill)
    return img


def _as_mask(img, dims) -> np.ndarray:
    return np.asarray(pv.wrap(img).point_data.active_scalars).reshape(
        dims, order="F") > 0


def _wrap_image(mask: np.ndarray, origin, dims, voxel: float,
                sigma: float = 0.0) -> pv.ImageData:
    """Маска як обʼєм для ізоповерхні.

    `sigma` > 0 — гаусове згладжування поля У ВОКСЕЛЯХ. Це не косметика:
    ізоповерхня бінарної маски ступає по вокселях, і на тонкій кромці крила
    видно пилку. Поле float зі згладжуванням дає плавний перетин, і — що
    неочевидно — воно ще й ТОЧНІШЕ: на plane7 зсув поверхні 0.46 мм проти
    0.47, максимум 2.5 замість 2.7, трикутників 182 тис. замість 198 тис.

    Обовʼязково float: на uint8 гаус округляє все назад у 0/1, ізоповерхня
    0.5 не знаходить нічого, і результат порожній.
    """
    img = pv.ImageData(dimensions=dims, spacing=(voxel,) * 3,
                       origin=tuple(float(v) for v in origin))
    if sigma <= 0:
        img.point_data["solid"] = mask.astype(np.uint8).ravel(order="F")
        img.set_active_scalars("solid")
        return img
    img.point_data["solid"] = mask.astype(np.float32).ravel(order="F")
    img.set_active_scalars("solid")
    blur = vtk.vtkImageGaussianSmooth()
    blur.SetInputData(img)
    blur.SetStandardDeviations(sigma, sigma, sigma)
    blur.SetRadiusFactors(2.5, 2.5, 2.5)
    blur.Update()
    out = pv.wrap(blur.GetOutput())
    out.set_active_scalars(out.point_data.keys()[0])
    return out


def _parity_fill(mesh, origin, dims, voxel) -> np.ndarray:
    """Заливка «всередині за парністю перетинів» — матеріал тіла."""
    img = _blank(origin, dims, voxel, fill=1)
    stencil = vtk.vtkPolyDataToImageStencil()
    stencil.SetInputData(mesh)
    stencil.SetOutputOrigin(img.GetOrigin())
    stencil.SetOutputSpacing(img.GetSpacing())
    stencil.SetOutputWholeExtent(img.GetExtent())
    stencil.SetTolerance(0.0)
    stencil.Update()
    paint = vtk.vtkImageStencil()
    paint.SetInputData(img)
    paint.SetStencilData(stencil.GetOutput())
    paint.ReverseStencilOff()
    paint.SetBackgroundValue(0)
    paint.Update()
    return _as_mask(paint.GetOutput(), dims)


def _shell_voxels(mesh, origin, dims, voxel) -> np.ndarray:
    """Воксели, крізь які проходить сама поверхня. Орієнтації не потребує.

    `SetMaximumDistance` у VTK — НЕ відстань, а ЧАСТКА габариту моделі,
    обрізана до 1. Тут довго стояв сам воксель у метрах, і це працювало
    випадково: на моделі в пів метра частка виходила близько вокселя. Але
    модель у 514 «метрів» (міліметровий файл при вибраних метрах) давала
    1.96 → 1.0, тобто кожен із 227 тис. трикутників обходив усі 6.6 млн
    вокселів — програма «висла» на годину. А на дрібних тілах навпаки:
    зона пошуку 0.2 вокселя губила оболонку, хрест циліндрів 10 см мав
    силует на 2.5% менший.

    Сам растр від цього параметра не залежить, щойно зона ≥ одного вокселя:
    позначаються ті самі воксели (перевірено — 1, 1.5 і 2 вокселі дають
    той самий масив). Тому ставимо рівно один воксель як частку габариту:
    силует кулі +0.33% і хреста −0.1% на будь-якому масштабі, 0.1–0.3 с.
    """
    model = vtk.vtkVoxelModeller()
    model.SetInputData(mesh)
    model.SetSampleDimensions(*dims)
    model.SetModelBounds(
        float(origin[0]), float(origin[0] + (dims[0] - 1) * voxel),
        float(origin[1]), float(origin[1] + (dims[1] - 1) * voxel),
        float(origin[2]), float(origin[2] + (dims[2] - 1) * voxel))
    model.SetScalarTypeToUnsignedChar()
    model.SetForegroundValue(1)
    model.SetBackgroundValue(0)
    span = max((d - 1) * voxel for d in dims)
    model.SetMaximumDistance(min(1.0, voxel / span) if span > 0 else 1.0)
    model.Update()
    return _as_mask(model.GetOutput(), dims)


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """Розширення маски на `radius` вокселів по кожній осі.

    np.roll замикає масив по колу, і це тут безпечно лише завдяки запасу по
    краях сітки (`_voxel_grid`): оболонка ніколи не підходить до межі
    ближче ніж на кілька вокселів, тому загорнутися їй нікуди.
    """
    out = mask.copy()
    for axis in range(3):
        for shift in range(1, radius + 1):
            out |= np.roll(mask, shift, axis=axis)
            out |= np.roll(mask, -shift, axis=axis)
    return out


def _flood_outside(free: np.ndarray, origin, dims, voxel) -> np.ndarray:
    """Куди дістає повітря з кутка сітки, рухаючись лише по `free`."""
    img = _wrap_image(free, origin, dims, voxel)
    seeds = vtk.vtkPolyData()
    pts = vtk.vtkPoints()
    pts.InsertNextPoint(float(origin[0] + voxel * 0.01),
                        float(origin[1] + voxel * 0.01),
                        float(origin[2] + voxel * 0.01))
    seeds.SetPoints(pts)
    conn = vtk.vtkImageConnectivityFilter()
    conn.SetInputData(img)
    conn.SetSeedData(seeds)
    conn.SetExtractionModeToSeededRegions()
    conn.SetLabelModeToConstantValue()
    conn.SetLabelConstantValue(1)
    conn.SetScalarRange(1, 1)
    conn.Update()
    return _as_mask(conn.GetOutput(), dims)


def voxel_wrap(mesh: pv.PolyData, voxel: float, closing: int = 0,
               smooth: int = 10, sigma: float = 0.8,
               limit: int = 60_000_000) -> pv.PolyData:
    """Обгортає геометрію воксельною сіткою — оболонка гарантовано замкнена.

    Це те, що в слайсерах робить «Fix through Netfabb»: модель не лагодять
    по трикутниках, а перерізають наново. Ізоповерхня бінарного обʼєму
    замкнена завжди — звідси й гарантія.

    ДВА КРИТЕРІЇ «ТІЛА», і потрібні обидва, бо кожен окремо має свою сліпу
    пляму:

    * **Парність перетинів** (`vtkPolyDataToImageStencil`) дає матеріал
      тіла. Сліпа пляма — подвійні стінки: на двох суміщених копіях однієї
      поверхні парність гасне й не лишається НІЧОГО (виміряно: порожній
      результат на двох накладених сферах).
    * **Заливка ззовні** по вокселях, крізь які не проходить поверхня, дає
      все, куди не дістає повітря. Сліпа пляма — дірки: крізь дірку, більшу
      за воксель, повітря заливає нутрощі.

    Тому тіло = матеріал ∪ замкнені порожнини. Відкриті канали (повітрозабір,
    отвір наскрізь) лишаються відкритими — повітря до них дістає, — а
    замкнений порожній фюзеляж заливається. Для обтікання це саме те, що
    треба: нутрощі тіла не є текучим середовищем.

    Виміряно на `plane7` (267 тис. трикутників, воксель 3 мм): частин 93 → 59
    проти чистої парности, зсув поверхні 0.64 мм по медіані; на `plane6`
    (775 тис.) — 211 → 137 частин, зсув 0.67 мм. Обʼєм при цьому виходить
    БІЛЬШИЙ за той, що дає парність (0.0056 проти 0.0029 м³ на `plane7`), і
    це не помилка: модель друкована, зі товщиною стінки, і парність рахує
    лише матеріал, а обтікання цікавить зовнішня оболонка.

    Ціна чесна й непомітною не буває: зникає все, дрібніше за воксель,
    заростають щілини, вужчі за воксель, а поверхня зсувається приблизно на
    пів вокселя. Тому це не «краща кнопка ремонту», а обмін деталей на
    замкненість — доречний тоді, коли деталі все одно дрібніші за комірку
    сітки.
    """
    if mesh.n_cells == 0:
        raise ValueError(tr("The surface is empty."))
    smallest, _ = voxel_limits(mesh, limit)
    if voxel < smallest:
        raise ValueError(
            tr("Voxel {voxel:.6g} needs too much memory: with it the "
               "grid will exceed {v:.0f} M nodes. The finest allowed is "
               "{smallest:.6g}.", voxel=voxel, v=limit / 1e6, smallest=smallest))

    origin, dims = _voxel_grid(mesh, voxel)
    material = _parity_fill(mesh, origin, dims, voxel)
    shell = _shell_voxels(mesh, origin, dims, voxel)

    solid = material
    # Типово оболонку не товщимо: так поверхня лишається там, де вона є.
    # Товщимо лише тоді, коли інакше не вийшло нічого — тобто коли парність
    # згасла на подвійних стінках, а в растровій оболонці є проколи, крізь
    # які заливка витекла всередину.
    for radius in ((closing,) if closing else (0, 1, 2)):
        thick = shell if radius <= 0 else _dilate(shell, radius)
        air = _flood_outside(~thick, origin, dims, voxel)
        # Оболонка сама повітря не пропускає, тому «недосяжне» включає і її
        # ЗОВНІШНІЙ шар. Його треба виключити, інакше поверхня поїде назовні
        # на товщину растру (виміряно: зсув 4.4 мм замість 0.6 при вокселі
        # 3 мм). Зовнішній шар — це ті воксели оболонки, що межують із
        # досяжним повітрям; решта оболонки лежить усередині тіла, і її,
        # навпаки, треба лишити: без цього навколо кожної замкненої
        # порожнини зостається щілина в один воксель, і контур плодить
        # внутрішні оболонки (виміряно: 10 частин замість однієї).
        outer = thick & _dilate(air, 1)
        candidate = material | ((~air) & (~outer))
        if candidate.any():
            solid = candidate
            break

    if not solid.any():
        raise ValueError(
            tr("Nothing to wrap: the model has no closed volume at least "
            "one voxel thick. This happens with a flat surface and with "
            "a model smaller than the voxel itself — use a smaller one."))

    field = _wrap_image(solid, origin, dims, voxel, sigma=sigma)
    surf = field.contour([0.5], scalars=field.point_data.keys()[0],
                        method="flying_edges")
    surf = drop_stray_cells(surf.triangulate())
    if surf.n_cells == 0:
        raise ValueError(
            tr("No surface is left after wrapping — the voxel is larger "
            "than the model itself. Use a smaller one."))
    if smooth > 0:
        # Тейбін, а не лаплас: лаплас стягує тіло до точки, а цей тримає
        # обʼєм, і крило не худне з кожним проходом. Десяти проходів досить,
        # бо основну пилку вже прибрало згладжування поля.
        surf = surf.smooth_taubin(n_iter=int(smooth), pass_band=0.1,
                                  feature_angle=60.0,
                                  boundary_smoothing=False)
        surf = drop_stray_cells(surf.triangulate())
    return orient_parts_outward(surf)


# --------------------------------------------------------------------------
# Запис у кейс
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Тонкі місця: знайти, показати, зрізати
# --------------------------------------------------------------------------
#
# ЧОМУ ЦЕ ВЗАГАЛІ ПОТРІБНО. snappyHexMesh ділить комірку навпіл і тільки
# навпіл, тож розмір на поверхні — це база/2^рівень. Деталь, тонша за
# комірку, в такій сітці існувати не може: дві її протилежні поверхні
# мусили б опинитися в одній комірці, а між ними нема чого покласти.
#
# На справжньому гвинті 8x4.5 це виглядало так: медіанна товщина лопаті
# 1.46 мм, чверть поверхні тонша за 0.52 мм, задня кромка сходить у нуль.
# Сітка з коміркою 4.41 мм узяла з лопаті 156 см² зі 187.8 — зникло 17%, і
# checkMesh на це сказав «Mesh OK».
#
# ЩО РОБИТЬ ЦЕЙ КОД. Не «покращує» геометрію й не згладжує її. Він знаходить
# грані, під якими металу менше за поріг, і прибирає РІВНО ЇХ, а діру
# заліплює. Усі грані, що лишилися, — ті самі трикутники, що й були, точка
# в точку. Саме тому поруч стоїть перевірка `trim_report`: вона доводить
# це числом, а не обіцянкою.


def thickness_below(mesh: pv.PolyData, limit: float,
                    steps: int = 28) -> np.ndarray:
    """Товщина матеріалу під кожною гранню, м. Більше за `limit` — inf.

    Рахується маршем усередину по нормалі: від центра грані крокуємо проти
    нормалі й дивимося, де знакова відстань до поверхні стане додатною —
    тобто де ми вийшли з тіла. Перший такий крок і є товщина.

    Промені трасувати по одному не можна: на 311 тисячах граней це десятки
    хвилин. Тому крок робиться ОДРАЗУ ДЛЯ ВСІХ граней, а всередині —
    єдиний виклик VTK на всю хмару точок. Двадцять вісім кроків на 311к
    граней — це 8.7 млн запитів, і вони йдуть секунди.

    `limit` — не оптимізація, а зміст: питання не «яка товщина», а «чи
    тонше за комірку». Усе, що товще, однаково придатне, і міряти його
    точніше немає навіщо.
    """
    if mesh.n_cells == 0:
        return np.zeros(0)
    m = mesh.compute_normals(cell_normals=True, point_normals=False,
                             auto_orient_normals=True, consistent_normals=True)
    centres = np.asarray(m.cell_centers().points, dtype=float)
    normals = np.asarray(m.cell_data["Normals"], dtype=float)

    def outside(idx: np.ndarray, depth: np.ndarray) -> np.ndarray:
        """Чи вийшли ці грані з тіла на такій глибині."""
        cloud = pv.PolyData(centres[idx] - normals[idx] * depth[:, None])
        return np.asarray(
            cloud.compute_implicit_distance(mesh)["implicit_distance"]) > 0.0

    out = np.full(len(centres), np.inf)
    if len(centres) == 0:
        return out

    # СПЕРШУ ПИТАЄМО ПРО САМУ ГЛИБИНУ ПОРОГА, а не крокуємо від нуля.
    #
    # Товстих граней більшість — на лопаті це 83%, — і для них відповідь
    # відома з одного запиту: якщо на глибині `limit` ми все ще в металі,
    # далі міряти нема чого, там товще за поріг. Лінійний марш цього не
    # знав і гнав УСІ грані крізь усі двадцять вісім кроків: 25 секунд на
    # деталь, тобто попередній перегляд, якого ніхто не дочекається.
    all_idx = np.arange(len(centres))
    eps = max(limit * 1e-4, 1e-9)
    thin = all_idx[outside(all_idx, np.full(len(all_idx), limit))]
    if not len(thin):
        return out

    # Решту — двійковим пошуком. Він дає ту саму роздільність за вчетверо
    # менше кроків: дванадцять поділів це limit/4096.
    lo = np.full(len(thin), eps)
    hi = np.full(len(thin), limit)
    for _ in range(max(4, steps // 2)):
        mid = 0.5 * (lo + hi)
        left = outside(thin, mid)
        hi = np.where(left, mid, hi)
        lo = np.where(left, lo, mid)
    out[thin] = hi
    return out


def _face_neighbours(mesh: pv.PolyData) -> tuple[np.ndarray, np.ndarray]:
    """Пари сусідніх граней (через спільне ребро).

    Сусідство рахуємо самі. Спокуса взяти `extract_feature_edges` не
    спрацьовує: він віддає ЛІНІЇ, а до ліній нічим поміряти відстань —
    `compute_implicit_distance` хоче полігони й на лініях лише сипле
    «No polygons to evaluate function».
    """
    faces = _tri_faces(mesh)
    if faces is None or len(faces) == 0:
        return np.zeros(0, dtype=int), np.zeros(0, dtype=int)
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    e = np.sort(e, axis=1)
    owner = np.tile(np.arange(len(faces)), 3)
    order = np.lexsort((e[:, 1], e[:, 0]))
    e, owner = e[order], owner[order]
    twin = np.flatnonzero((e[1:] == e[:-1]).all(axis=1))
    return owner[twin], owner[twin + 1]


def _grow(mask: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Розширити вибір на сусідів."""
    out = mask.copy()
    out[a[mask[b]]] = True
    out[b[mask[a]]] = True
    return out


def smooth_mask(mesh: pv.PolyData, mask: np.ndarray,
                rounds: int = 2) -> np.ndarray:
    """Прибрати з вибору цятки й зазубні — морфологією по сусідству граней.

    Поріг по товщині сам собою дає рвану межу: товщина хитається навколо
    порога, і вибір стрибає з трикутника на трикутник. Зрізане по такій
    межі лишає сходи, а не фаску — це видно тільки очима, числа при цьому
    гарні.

    Спершу СТИСКАЄМО (прибирає поодинокі цятки), потім РОЗШИРЮЄМО назад
    (повертає межу на місце й заповнює зазубні). Так само працює
    морфологічне відкриття на зображенні, і тут воно те саме, лише граф
    замість решітки.
    """
    if not len(mask) or rounds <= 0:
        return mask
    a, b = _face_neighbours(mesh)
    if not len(a):
        return mask
    out = mask
    for _ in range(rounds):          # стиснути
        out = ~_grow(~out, a, b)
    for _ in range(rounds * 2):      # розширити назад і ще на крок
        out = _grow(out, a, b)
    for _ in range(rounds):          # і знову стиснути — межа стає гладкою
        out = ~_grow(~out, a, b)
    return out


def sharp_faces(mesh: pv.PolyData, included_deg: float = 60.0) -> np.ndarray:
    """Маска граней, що сходяться в ГОСТРУ кромку (кут між ними менший).

    Потрібна, щоб відрізнити задню кромку від передньої. Обидві тонкі, але
    роблять протилежне: задня сходить клином у нуль і сітці не дається, а
    передня — круглий носок, і саме на ньому живе пік розрідження, який
    створює підйомну силу. Притупити його означає зіпсувати розвʼязок
    заради сітки.

    Клин від носка відрізняє кут МІЖ ПОВЕРХНЯМИ. У клина він малий, у
    круглого носка великого кута ніде немає — там усе гладко. VTK міряє кут
    між НОРМАЛЯМИ, а він доповнює наш до 180°, тому поріг і перевертається.
    """
    faces = _tri_faces(mesh)
    if faces is None or len(faces) == 0:
        return np.zeros(mesh.n_cells, dtype=bool)
    mask = np.zeros(len(faces), dtype=bool)

    # Сусідство рахуємо самі, через спільні ребра. Спокуса взяти
    # `extract_feature_edges` тут не спрацьовує: він віддає ЛІНІЇ, а
    # відстань до них нічим поміряти — `compute_implicit_distance` хоче
    # полігони й на лініях лише сипле «No polygons to evaluate function».
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    e = np.sort(e, axis=1)
    owner = np.tile(np.arange(len(faces)), 3)
    order = np.lexsort((e[:, 1], e[:, 0]))
    e, owner = e[order], owner[order]
    twin = np.flatnonzero((e[1:] == e[:-1]).all(axis=1))
    if not len(twin):
        return mask

    m = mesh.compute_normals(cell_normals=True, point_normals=False,
                             auto_orient_normals=True, consistent_normals=True)
    n = np.asarray(m.cell_data["Normals"], dtype=float)
    a, b = owner[twin], owner[twin + 1]
    cos = (n[a] * n[b]).sum(axis=1)
    # Кут МІЖ ПОВЕРХНЯМИ доповнює кут між нормалями до 180°: у клина з
    # розхилом 10° нормалі дивляться майже назустріч. Тому поріг і
    # перевертається.
    sharp = cos < math.cos(math.radians(180.0 - included_deg))
    mask[a[sharp]] = True
    mask[b[sharp]] = True
    return mask


@dataclass
class TrimReport:
    """Що саме зрізано — і доказ, що більше нічого не чіпали."""
    cut_faces: int = 0
    kept_faces: int = 0
    cut_area: float = 0.0
    area_before: float = 0.0
    area_after: float = 0.0
    thinnest: float = 0.0
    limit: float = 0.0
    patched: int = 0             # граней у латках
    watertight_before: bool = False
    watertight_after: bool = False
    moved: float = 0.0           # найбільший зсув уцілілої точки, м
    refused: str = ""            # чому не різали

    @property
    def cut_share(self) -> float:
        return self.cut_area / self.area_before if self.area_before else 0.0

    def lines(self) -> list[str]:
        if self.refused:
            return [self.refused]
        return [
            tr("cut {cut_faces} faces of {v} ({v2:.1f}% of the area)", cut_faces=self.cut_faces, v=self.cut_faces + self.kept_faces, v2=self.cut_share * 100),
            tr("area {v:.1f} -> {v2:.1f} cm²", v=self.area_before * 1e4, v2=self.area_after * 1e4),
            tr("the thinnest spot was {v:.2f} mm at a threshold of {v2:.2f} mm", v=self.thinnest * 1000, v2=self.limit * 1000),
            tr("patched {patched} faces", patched=self.patched),
            tr("watertight: {v} -> {v2}", v='was' if self.watertight_before else 'was NOT', v2='is' if self.watertight_after else 'is NOT'),
            tr("the rest of the surface moved by {v:.0f} µm", v=self.moved * 1e6),
        ]


def bevel_thin(mesh: pv.PolyData, limit: float, width: float = 0.0,
               max_share: float = 0.12) -> tuple[pv.PolyData, TrimReport]:
    """Зрізати СМУГУ тонкого вздовж кромки й залатати — фаска, як у CAD.

    `limit` — товщина, нижче якої сітка поверхню не відтворює (звичайно
    комірка на поверхні). `width` — наскільки глибоко від межі з товстим
    металом заходити. Нуль означає «скільки треба», тобто всю тонку
    область; це майже завжди не те, що потрібно.

    ЧОМУ ШИРИНА ВЗАГАЛІ ПОТРІБНА, і це головне в усій функції. Перша
    версія різала ПРОСТО ВСЕ, тонше за поріг. На справжній лопаті 8x4.5
    вийшла рвана кукса: кінець лопаті тонкий не кромкою, а цілком, і під
    ніж пішов увесь. Числа при цьому були бездоганні — уціліла поверхня
    зсунулася на нуль мікронів, габарит не змінився, — а геометрія стала
    непридатна. Побачити це можна було тільки очима.

    Смуга лічиться від МЕЖІ З ТОВСТИМ: беремо тонкі грані, до яких від
    товстого металу не далі ніж `width`. Тоді клин задньої кромки
    зрізається (товсте поруч), а суцільно тонкий кінець лишається цілим
    (товстого поруч немає) — саме так, як робить фаска в CAD і бевел у
    Blender, які теж ідуть ВІД ребра, а не заливають область.

    Повертає (поверхня, звіт). Уцілілі грані — ті самі трикутники, що й
    були; звіт доводить це полем `moved`.
    """
    rep = TrimReport(limit=limit)
    if mesh.n_cells == 0 or limit <= 0:
        rep.refused = tr("nothing to cut")
        return mesh, rep

    area = np.asarray(
        mesh.compute_cell_sizes(length=False, volume=False)["Area"])
    rep.area_before = float(area.sum())
    rep.watertight_before = _count_edges(mesh, boundary_edges=True,
                                         feature_edges=False,
                                         manifold_edges=False,
                                         non_manifold_edges=False) == 0

    thin = thickness_below(mesh, limit)
    mask = np.isfinite(thin)
    if not mask.any():
        rep.refused = (tr("there are no spots thinner than {v:.2f} mm — "
                          "the geometry already fits this cell", v=limit * 1000))
        rep.kept_faces = mesh.n_cells
        rep.area_after = rep.area_before
        rep.watertight_after = rep.watertight_before
        return mesh, rep
    rep.thinnest = float(thin[mask].min())

    if width > 0 and (~mask).any():
        # Відстань кожної тонкої грані до ТОВСТОГО металу. Він полігональний,
        # тож `compute_implicit_distance` тут працює (на відміну від
        # відстані до ребер, які лінії й нічого не дають).
        thick = mesh.extract_cells(np.flatnonzero(~mask)).extract_surface()
        if thick.n_cells:
            centres = np.asarray(mesh.cell_centers().points)[mask]
            far = np.abs(np.asarray(
                pv.PolyData(centres)
                .compute_implicit_distance(thick)["implicit_distance"]))
            near = np.zeros(len(mask), dtype=bool)
            near[np.flatnonzero(mask)] = far <= width
            mask = near
            if not mask.any():
                rep.refused = (
                    tr("a band {v:.2f} mm wide caught nothing: the thin "
                       "areas here do not border thick ones, so this is "
                       "not an edge. A chamfer will not help here.", v=width * 1000))
                return mesh, rep

    # ЗАПОБІЖНИК — ПЕРЕД ЗГЛАДЖУВАННЯМ, і порядок тут не косметика.
    #
    # Спершу було навпаки, і на суцільно тонкій деталі згладжування
    # зʼїдало вибір раніше, ніж запобіжник устигав спрацювати. Людина
    # діставала «тонке розсіяне цятками» замість «це вся деталь» —
    # тобто відмову з неправильною причиною, а причина тут і є вся
    # користь від відмови.
    rep.cut_area = float(area[mask].sum())
    if rep.cut_share > max_share:
        rep.refused = (
            tr("{v:.0f}% of the area falls under the cut — this is not "
               "an edge, this is the whole part. Cutting would mean "
               "removing the part itself: this needs a finer cell or a "
               "different model. Try a smaller chamfer width.", v=rep.cut_share * 100))
        return mesh, rep

    # Межу вибору згладжуємо ПЕРЕД різанням: інакше зріз іде по зубцях і
    # лишає сходи замість фаски.
    mask = smooth_mask(mesh, mask, rounds=2)
    if not mask.any():
        rep.refused = (tr("after smoothing the boundary, nothing is left to "
                       "cut — the thin areas here are scattered in "
                       "spots, not in a band"))
        return mesh, rep
    rep.cut_area = float(area[mask].sum())

    keep = np.flatnonzero(~mask)
    out = mesh.extract_cells(keep).extract_surface().triangulate()
    rep.cut_faces = int(mask.sum())
    before_patch = out.n_cells

    # Діру заліплюємо тим, що вже є в модулі: fill_holes сам вирівнює
    # нормали. Розмір латки обмежуємо габаритом — брати «скільки завгодно»
    # означало б дозволити заліпити й те, що дірою не було.
    span = max(mesh.bounds[1] - mesh.bounds[0],
               mesh.bounds[3] - mesh.bounds[2],
               mesh.bounds[5] - mesh.bounds[4])
    if _count_edges(out, boundary_edges=True, feature_edges=False,
                    manifold_edges=False, non_manifold_edges=False):
        try:
            out = fill_holes(out, span * 0.5)
        except Exception:                                    # noqa: BLE001
            pass
    rep.patched = max(0, out.n_cells - before_patch)
    rep.kept_faces = before_patch
    rep.area_after = float(np.asarray(
        out.compute_cell_sizes(length=False, volume=False)["Area"]).sum())
    rep.watertight_after = _count_edges(out, boundary_edges=True,
                                        feature_edges=False,
                                        manifold_edges=False,
                                        non_manifold_edges=False) == 0

    # ДОКАЗ, ЩО ЗАЙВОГО НЕ ЧІПАЛИ. Кожна вціліла точка має лежати там-таки,
    # де й була. Міряємо відстань до ВИХІДНОЇ поверхні: нуль означає, що
    # нічого не зсунули, не згладили й не перебудували.
    if out.n_points:
        rep.moved = surface_deviation(out, mesh)[1]
    return out, rep


def thin_share(mesh: pv.PolyData, limit: float) -> float:
    """Частка площі, під якою металу менше за `limit`.

    Одне число, яким міряється вся ця морока: скільки поверхні сітка з
    такою коміркою відтворити не може. На вихідному корпусі при комірці
    4.41 мм воно дорівнювало 0.796 — тобто чотири пʼятих моделі.

    Саме його й порівнюють ДО і ПІСЛЯ будь-якої правки геометрії. Питати
    «чи змінилося» тут мало: правка, від якої тонкого побільшало, теж
    змінює, і без цього числа виглядає як робота.
    """
    if mesh.n_cells == 0 or limit <= 0:
        return 0.0
    area = np.asarray(
        mesh.compute_cell_sizes(length=False, volume=False)["Area"])
    total = float(area.sum())
    if total <= 0:
        return 0.0
    mask = np.isfinite(thickness_below(mesh, limit))
    return float(area[mask].sum()) / total


def thin_polydata(mesh: pv.PolyData, limit: float) -> pv.PolyData:
    """Самі лише тонкі грані — щоб показати їх у вювпорті, перш ніж різати.

    Дивитися на те, що збираєшся прибрати, треба до того, а не після.
    """
    if mesh.n_cells == 0:
        return pv.PolyData()
    mask = np.isfinite(thickness_below(mesh, limit))
    if not mask.any():
        return pv.PolyData()
    return mesh.extract_cells(np.flatnonzero(mask)).extract_surface()


def windward_faces(mesh: pv.PolyData, direction, cos_limit: float = 0.0):
    """Грані, на які набігає потік, — і трохи далі за них.

    Критерій — косинус кута між зовнішньою нормаллю грані та напрямком
    потоку. Строго лобова грань дає -1, бічна 0, підвітряна +1.

    Поріг узятий НЕ на нулі, тобто не «рівно навітряні», і це важливо.
    У точці гальмування на самій кромці тиск високий і рівний, рахувати
    його неважко. А пік РОЗРІДЖЕННЯ, який робить більшу частину підйомної
    сили, сидить уже за кромкою, на верхній поверхні, де нормаль дивиться
    рівно вбік — косинус нуль. За порогом на нулі ця зона не потрапила б у
    згущення взагалі, тобто згустили б саме те місце, де й так усе просто.
    Але й надто щедрий поріг марний. На тонкому крилі верх і низ мають
    нормаль майже впоперек потоку, косинус близько нуля, тому поріг 0.17
    відбирає 97% тіла — тобто не відбирає нічого. Нуль на тому ж літаку
    бере 59% граней і накриває на крилі передні 32% хорди: і точку
    гальмування, і розгін за нею. Це й лишається типовим.

    Повертає PolyData з відібраних граней; порожню, якщо не відібралося
    нічого. Замкненою вона не буде — і не має бути: для mode distance
    у snappyHexMesh замкненість не потрібна.
    """
    if mesh is None or mesh.n_cells == 0:
        return pv.PolyData()
    d = np.asarray(direction, dtype=float)
    n = np.linalg.norm(d)
    if n <= 0:
        return pv.PolyData()
    d = d / n
    normals = np.asarray(
        mesh.compute_normals(cell_normals=True, point_normals=False,
                             auto_orient_normals=True, consistent_normals=True)
        .cell_data["Normals"], dtype=float)
    cos = normals @ d
    sel = np.flatnonzero(cos < cos_limit)
    if sel.size == 0:
        return pv.PolyData()
    return mesh.extract_cells(sel).extract_surface()


def write_stl(mesh: pv.PolyData, path: str, binary: bool = True) -> None:
    """Пише STL для constant/triSurface. Геометрія вже має бути в метрах."""
    mesh.save(path, binary=binary)


def leak_advice(d: Diagnosis, surface_cell: float) -> tuple[str, str]:
    """Чи протече сітка крізь дірки в STL при заданому розмірі комірки.

    Дірку, дрібнішу за комірку, snappyHexMesh затягує сам: усі комірки, що
    перетинають поверхню навколо неї, він однаково видаляє. Більша дірка стає
    наскрізним каналом, і заливка від locationInMesh заповнює нутрощі тіла.
    """
    from fs_physics import fmt_len

    if not d.hole_sizes:
        return ("ok", "")
    if surface_cell <= 0:
        return ("warn", tr("Unknown cell size."))

    biggest = d.largest_hole
    ratio = biggest / surface_cell
    n_big = sum(1 for s in d.hole_sizes if s > surface_cell)

    if ratio > 1.0:
        return ("bad", (
            tr("It will leak: the largest hole {biggest} is {ratio:.1f} "
               "cells ({n_big} holes are larger than a cell). Close the "
               "holes or use a coarser cell on the surface.", biggest=fmt_len(biggest), ratio=ratio, n_big=n_big)
        ))
    if ratio > 0.5:
        return ("warn", (
            tr("Borderline: the largest hole {biggest} ≈ {ratio:.2f} "
               "cells. It holds now, but it will break after the mesh is "
               "refined.", biggest=fmt_len(biggest), ratio=ratio)
        ))
    return ("ok", (
        tr("Largest hole {biggest} — {ratio:.2f} cells, snappy will close it.", biggest=fmt_len(biggest), ratio=ratio)
    ))


def tesselation_advice(d: Diagnosis, surface_cell: float) -> tuple[str, str]:
    """Чи достатньо дрібна тесселяція для заданого розміру поверхневої комірки.

    Правило: трикутник має бути помітно меншим за комірку, інакше snap
    притягнеться до фасеток і шари не наростуть.

    Міряємо найкоротшим ребром, а не медіанним: у лофтованих тіл трикутники
    витягнуті, і медіана показує крок між перерізами, а не дрібність опису
    форми. Через це рівне крило з чудово переданим профілем виглядало як
    «трикутники грубші за комірку».
    """
    edge = d.short_edge or d.median_edge
    if surface_cell <= 0 or edge <= 0:
        return ("ok", "")
    ratio = edge / surface_cell
    if ratio > 2.0:
        return ("bad", (
            tr("Triangles are coarser than the cell ({ratio:.1f}x). Snap "
               "will sit on the facets, the boundary layer will not "
               "grow. Refine the STL or use a larger cell.", ratio=ratio)
        ))
    if ratio > 1.0:
        return ("warn", (
            tr("Short edge ≈ {ratio:.1f} cells. Sharp spots will be stepped.", ratio=ratio)
        ))
    if ratio < 0.05:
        return ("warn", (
            tr("The tessellation is ten times finer than the cell "
               "({ratio:.3f}). It will only slow down the search — you "
               "can decimate it.", ratio=ratio)
        ))
    return ("ok", tr("Short edge ≈ {ratio:.2f} cells — good.", ratio=ratio))


# --------------------------------------------------------------------------
# Гвинт із параметрів
# --------------------------------------------------------------------------

def make_propeller(diameter: float, blades: int = 2, pitch: float = 0.10,
                   hub_diameter: float = 0.0, chord: float = 0.0,
                   thickness: float = 0.10, centre=(0.0, 0.0, 0.0),
                   axis: str = "x", stations: int = 14,
                   around: int = 28, hand: str = "cw") -> pv.PolyData:
    """Гвинт із чисел: діаметр, лопаті, крок, втулка.

    РУКА (`hand`) — не косметика, а умова того, щоб гвинт узагалі тягнув.
    Лопать закручена в один бік, і вона тягне вперед лише тоді, коли
    крутиться в ПОГОДЖЕНОМУ з цією закруткою напрямку:

        hand="cw"   тягне вперед при ДОДАТНИХ обертах навколо осі
        hand="ccw"  дзеркальна їй — тягне вперед при ВІДʼЄМНИХ

    На співвісній парі чи на літаку з протилежним обертанням обидва
    гвинти мають тягнути вперед, тож і руки в них різні. Якщо взяти
    ту саму лопать і просто дати їй відʼємні оберти, вона стане
    ВІТРЯКОМ: тяга піде назад, момент теж, а помилки не буде жодної —
    числа просто вийдуть зі знаком мінус, і це легко списати на
    «щось не так із MRF».

    Дзеркальна лопать робиться зміною знака закрутки. Для гвинта з
    рівномірно розставленими лопатями це рівно дзеркальне відображення
    у площині, що містить вісь, — тобто саме те, що на складі лежить
    як «лівий гвинт».

    НАВІЩО. Зліпити гвинт руками з коробок легко неправильно — я пробував,
    і замість плаского диска вийшла фігура з габаритом 0.44 м уздовж осі.
    Помилка при цьому мовчазна: зона MRF накриє таку «геометрію» цілком
    законно, тяги не дасть, а помилки не буде.

    ЩО ЦЕ НАСПРАВДІ. Геометрично правильний гвинт, а не аеродинамічно
    оптимізована лопать. Закрутка тут рахується з КРОКУ за означенням:

        beta(r) = atan( P / (2 pi r) )

    тобто крок сталий по радіусу, як у найпростішого гвинта. Переріз —
    ЕЛІПС, а не профіль: він заокруглений з обох боків, і snappyHexMesh
    його бере без бою. Справжній профіль має гостру задню кромку, на якій
    сітка сідає на фасетки, а на надзвуку вона ще й дає сингулярність.

    Тому числа з такого гвинта беріть як ПОРЯДОК, а не як відповідь. Для
    справжніх — потрібна модель лопаті з CAD.

    Осі: типово гвинт дивиться вздовж +X, бо в цій програмі туди ж іде
    потік. Диск при цьому тонкий уздовж X і круглий у YZ — саме та форма,
    яку впізнає `rotor_zone_from_points`.
    """
    R = max(float(diameter), 1e-6) / 2.0
    n = max(int(blades), 1)
    hub_r = max(float(hub_diameter), 0.0) / 2.0
    if hub_r <= 0:
        hub_r = R * 0.18
    hub_r = min(hub_r, R * 0.6)
    c75 = float(chord) if chord > 0 else R * 0.24
    t_rel = min(max(float(thickness), 0.02), 0.5)
    # Невідоме слово — це не привід мовчки зробити правий гвинт: людина
    # вважатиме, що поставила лівий, і пара крутитиметься в один бік.
    key = str(hand).strip().lower()
    if key not in ("cw", "ccw"):
        raise ValueError(
            tr("the propeller hand must be 'cw' or 'ccw', not {hand!r}: "
               "'cw' pulls forward at positive rpm, 'ccw' — at negative "
               "rpm", hand=hand))
    hand_sign = -1.0 if key == "ccw" else 1.0

    def chord_at(x: float) -> float:
        """Хорда на відносному радіусі x = r/R.

        Планформа еліптична за формою, але не сходиться в нуль на кінці:
        нульова хорда дала б виродження трикутників, а це те, на що
        скаржиться власна ж діагностика програми.
        """
        base = math.sqrt(max(1.0 - (0.92 * x) ** 2, 0.02))
        return c75 * base / math.sqrt(max(1.0 - (0.92 * 0.75) ** 2, 0.02))

    def blade_points(psi_deg: float):
        """Точки однієї лопаті: [станція][навколо] у площині диска.

        Осі: радіус по Z, ВІСЬ ОБЕРТАННЯ ПО X, хорда при нульовій закрутці
        по Y. Тобто переріз лежить у площині обертання (YZ), а товщина
        дивиться вздовж вала — як у справжньої лопаті.

        ЧОМУ ЦЕ ВИПИСАНО ТАК ДОКЛАДНО. Перша версія будувала переріз у
        системі, де віссю обертання вважався Y, — а втулка, розстановка
        лопатей і `axis="x"` нижче вважали віссю X. Через цю розбіжність
        закрутка відкладалася ВІД ОСІ замість площини обертання, і кут
        виходив доповненням до прямого: 71 градус замість 18.8 на 0.75R.

        Мовчало все: поверхня замкнена, діаметр правильний, зона MRF
        будується, тяга є. Видно було лише по потужності — гвинт просив
        7.3 кВт замість 0.8 і мав ККД 11%, бо лопать стояла поперек
        потоку й гребла, а не летіла.
        """
        rows = []
        for i in range(stations + 1):
            x = i / stations
            r = hub_r + (R - hub_r) * x
            c = chord_at(r / R)
            t = c * t_rel
            # ЗНАК ЗАКРУТКИ І Є РУКА ГВИНТА. Дзеркальна лопать — це та
            # сама лопать із протилежним нахилом перерізу.
            beta = hand_sign * math.atan2(float(pitch),
                                          2.0 * math.pi * max(r, 1e-9))
            cb, sb = math.cos(beta), math.sin(beta)
            row = []
            for j in range(around):
                a = 2.0 * math.pi * j / around
                # Еліпс: уздовж хорди — півхорди, поперек — півтовщини.
                u = 0.5 * c * math.cos(a)
                v = 0.5 * t * math.sin(a)
                # Закрутка: поворот перерізу навколо радіуса (Z).
                # При beta = 0 хорда лягає в площину обертання (по Y), а
                # товщина — уздовж вала (по X). При beta = 90 навпаки.
                row.append((u * sb + v * cb, u * cb - v * sb, r))
            rows.append(row)
        # Поворот усієї лопаті навколо осі диска на кут розстановки.
        p = math.radians(psi_deg)
        cp, sp = math.cos(p), math.sin(p)
        return [[(px, py * cp - pz * sp, py * sp + pz * cp)
                 for px, py, pz in row] for row in rows]

    def solid_from_rows(rows) -> pv.PolyData:
        """Замкнена поверхня з набору кілець: боки плюс дві кришки."""
        pts, faces = [], []
        for row in rows:
            pts.extend(row)
        m, k = len(rows), around
        for i in range(m - 1):
            for j in range(k):
                a = i * k + j
                b = i * k + (j + 1) % k
                c = (i + 1) * k + (j + 1) % k
                d = (i + 1) * k + j
                faces += [3, a, b, c, 3, a, c, d]
        # Кришки: віяло від центра кільця. Без них тіло відкрите, і
        # snappyHexMesh протече крізь лопать наскрізь.
        for i, sign in ((0, True), (m - 1, False)):
            cx = tuple(sum(p[t] for p in rows[i]) / k for t in range(3))
            pts.append(cx)
            ci = len(pts) - 1
            for j in range(k):
                a = i * k + j
                b = i * k + (j + 1) % k
                faces += [3, ci, b, a] if sign else [3, ci, a, b]
        return pv.PolyData(np.asarray(pts, dtype=float),
                           np.asarray(faces, dtype=np.int64))

    parts = [solid_from_rows(blade_points(360.0 * i / n)) for i in range(n)]
    # Втулка: циліндр уздовж осі диска, трохи довший за товщину перерізу
    # біля кореня, щоб лопаті в нього справді входили, а не торкалися.
    hub_len = max(c75 * t_rel * 3.0, R * 0.08)
    # `.clean()` тут обовʼязковий, і це не про охайність.
    #
    # pv.Cylinder навіть із capping=True віддає кришки окремими точками:
    # після triangulate() бічна стінка й кришки НЕ ДІЛЯТЬ вершин, і
    # поверхня виходить незамкненою — 112 відкритих ребер на шві. Крізь
    # таку поверхню snappyHexMesh протікає, і побачити це можна аж у
    # сітці, де тіло раптом порожнє всередині.
    #
    # Спіймала це власна ж діагностика програми, коли гвинт уперше
    # потрапив у сцену.
    parts.append(pv.Cylinder(center=(0.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0),
                             radius=hub_r * 1.05, height=hub_len,
                             resolution=max(around, 24),
                             capping=True).triangulate().clean())

    prop = parts[0].merge(parts[1:], merge_points=False).triangulate()
    if axis == "y":
        prop = prop.rotate_z(90.0, point=(0, 0, 0), inplace=False)
    elif axis == "z":
        prop = prop.rotate_y(-90.0, point=(0, 0, 0), inplace=False)
    return prop.translate(tuple(float(v) for v in centre), inplace=False)
