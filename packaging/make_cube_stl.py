"""Write a cube (ASCII STL, 40 mm) for the smoke test of a built program.

    python packaging/make_cube_stl.py cube.stl
    "<program>" --smoke-test smoke.json cube.stl
"""
import sys

V = [(x, y, z) for x in (0, 40) for y in (0, 40) for z in (0, 40)]
# Two triangles for each face, outward normals (counterclockwise).
F = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
     (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]


def main(path: str) -> None:
    with open(path, "w", encoding="ascii") as fh:
        fh.write("solid cube\n")
        for a, b, c in F:
            fh.write(" facet normal 0 0 0\n  outer loop\n")
            for i in (a, b, c):
                fh.write("   vertex %g %g %g\n" % V[i])
            fh.write("  endloop\n endfacet\n")
        fh.write("endsolid cube\n")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "cube.stl")
