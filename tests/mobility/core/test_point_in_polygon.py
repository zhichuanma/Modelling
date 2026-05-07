from __future__ import annotations

import numpy as np

from mobility.core.spatial import _point_in_multipolygon, _point_in_polygon_with_holes


def test_square_contains_inside_outside_and_boundary_points() -> None:
    square = [np.array([[0.0, 0.0], [5.0, 0.0], [5.0, 5.0], [0.0, 5.0], [0.0, 0.0]])]

    assert _point_in_polygon_with_holes(1.0, 1.0, square)
    assert not _point_in_polygon_with_holes(6.0, 6.0, square)
    assert _point_in_polygon_with_holes(5.0, 5.0, square)


def test_polygon_hole_excludes_interior_and_hole_boundary() -> None:
    exterior = np.array([[0.0, 0.0], [6.0, 0.0], [6.0, 6.0], [0.0, 6.0], [0.0, 0.0]])
    hole = np.array([[2.0, 2.0], [4.0, 2.0], [4.0, 4.0], [2.0, 4.0], [2.0, 2.0]])

    assert not _point_in_polygon_with_holes(3.0, 3.0, [exterior, hole])
    assert _point_in_polygon_with_holes(1.0, 1.0, [exterior, hole])
    assert not _point_in_polygon_with_holes(2.0, 3.0, [exterior, hole])


def test_multipolygon_contains_either_disjoint_part() -> None:
    left = [np.array([[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]])]
    right = [np.array([[5.0, 5.0], [7.0, 5.0], [7.0, 7.0], [5.0, 7.0], [5.0, 5.0]])]

    assert _point_in_multipolygon(1.0, 1.0, [left, right])
    assert _point_in_multipolygon(6.0, 6.0, [left, right])
    assert not _point_in_multipolygon(3.0, 3.0, [left, right])
