"""Tests for the point_in_polygon validation function."""

import pytest

pytestmark = pytest.mark.django_db


class TestPointInPolygon:
    """Tests for the point_in_polygon validation function."""

    def test_point_inside_square(self) -> None:
        """Test point inside a simple square."""
        from events.schema import Coordinate2D, point_in_polygon

        square = [
            Coordinate2D(x=0, y=0),
            Coordinate2D(x=100, y=0),
            Coordinate2D(x=100, y=100),
            Coordinate2D(x=0, y=100),
        ]
        assert point_in_polygon(Coordinate2D(x=50, y=50), square) is True
        assert point_in_polygon(Coordinate2D(x=10, y=10), square) is True
        assert point_in_polygon(Coordinate2D(x=99, y=99), square) is True

    def test_point_outside_square(self) -> None:
        """Test point outside a simple square."""
        from events.schema import Coordinate2D, point_in_polygon

        square = [
            Coordinate2D(x=0, y=0),
            Coordinate2D(x=100, y=0),
            Coordinate2D(x=100, y=100),
            Coordinate2D(x=0, y=100),
        ]
        assert point_in_polygon(Coordinate2D(x=150, y=50), square) is False
        assert point_in_polygon(Coordinate2D(x=-10, y=50), square) is False
        assert point_in_polygon(Coordinate2D(x=50, y=150), square) is False
        assert point_in_polygon(Coordinate2D(x=50, y=-10), square) is False

    def test_point_inside_triangle(self) -> None:
        """Test point inside a triangle."""
        from events.schema import Coordinate2D, point_in_polygon

        triangle = [Coordinate2D(x=0, y=0), Coordinate2D(x=100, y=0), Coordinate2D(x=50, y=100)]
        assert point_in_polygon(Coordinate2D(x=50, y=30), triangle) is True
        assert point_in_polygon(Coordinate2D(x=50, y=50), triangle) is True

    def test_point_outside_triangle(self) -> None:
        """Test point outside a triangle."""
        from events.schema import Coordinate2D, point_in_polygon

        triangle = [Coordinate2D(x=0, y=0), Coordinate2D(x=100, y=0), Coordinate2D(x=50, y=100)]
        assert point_in_polygon(Coordinate2D(x=10, y=90), triangle) is False
        assert point_in_polygon(Coordinate2D(x=90, y=90), triangle) is False

    def test_point_inside_complex_polygon(self) -> None:
        """Test point inside a more complex L-shaped polygon."""
        from events.schema import Coordinate2D, point_in_polygon

        # L-shape
        l_shape = [
            Coordinate2D(x=0, y=0),
            Coordinate2D(x=50, y=0),
            Coordinate2D(x=50, y=50),
            Coordinate2D(x=100, y=50),
            Coordinate2D(x=100, y=100),
            Coordinate2D(x=0, y=100),
        ]
        assert point_in_polygon(Coordinate2D(x=25, y=25), l_shape) is True
        assert point_in_polygon(Coordinate2D(x=25, y=75), l_shape) is True
        assert point_in_polygon(Coordinate2D(x=75, y=75), l_shape) is True

    def test_point_outside_complex_polygon(self) -> None:
        """Test point outside a complex L-shaped polygon (in the cutout)."""
        from events.schema import Coordinate2D, point_in_polygon

        # L-shape with cutout in upper-right
        l_shape = [
            Coordinate2D(x=0, y=0),
            Coordinate2D(x=50, y=0),
            Coordinate2D(x=50, y=50),
            Coordinate2D(x=100, y=50),
            Coordinate2D(x=100, y=100),
            Coordinate2D(x=0, y=100),
        ]
        # Point in the "cutout" area of the L
        assert point_in_polygon(Coordinate2D(x=75, y=25), l_shape) is False

    def test_point_on_edge_behavior(self) -> None:
        """Test behavior of points on or near edges."""
        from events.schema import Coordinate2D, point_in_polygon

        square = [
            Coordinate2D(x=0, y=0),
            Coordinate2D(x=100, y=0),
            Coordinate2D(x=100, y=100),
            Coordinate2D(x=0, y=100),
        ]
        # Points very close to edges (just inside)
        assert point_in_polygon(Coordinate2D(x=1, y=50), square) is True
        assert point_in_polygon(Coordinate2D(x=99, y=50), square) is True
