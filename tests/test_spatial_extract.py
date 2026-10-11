"""Representative coordinate/provenance regressions for the Part A reader."""
import ezdxf
import pytest
from shapely.geometry import shape

from cad_domain.spatial_extract import extract_drawing


def test_nested_mirrored_instance_preserves_wcs_coordinates_and_source(tmp_path):
    doc = ezdxf.new("R2018")
    doc.layers.new("Site")
    inner = doc.blocks.new("Inner", base_point=(2, 1))
    outline = inner.add_lwpolyline(
        [(2, 1), (6, 1), (6, 4), (2, 4)], close=True,
        dxfattribs={"const_width": .2, "layer": "0", "elevation": 20},
    )
    inner.add_text("测试7#", dxfattribs={"height": 2, "insert": (3, 2, 20), "layer": "0"})
    outer = doc.blocks.new("Outer", base_point=(5, 7))
    child = outer.add_blockref(
        "Inner", (12, -3, 5), dxfattribs={"rotation": 90, "xscale": -1, "yscale": .5},
    )
    root = doc.modelspace().add_blockref(
        "Outer", (100, 200, 10),
        dxfattribs={"rotation": 30, "xscale": 2, "yscale": 3, "layer": "Site"},
    )
    path = tmp_path / "nested.dxf"
    doc.saveas(path)
    before = path.read_bytes()
    result = extract_drawing(path)

    assert path.read_bytes() == before
    assert len(result["polygons"]) == 1
    record = result["polygons"][0]
    polygon = shape(record["geometry"])
    # Independently calculated survey coordinates for the four transformed corners.
    expected = [
        (127.124355652982, 181.019237886467),
        (133.124355652982, 170.626933041054),
        (130.526279441629, 169.126933041054),
        (124.526279441629, 179.519237886467),
    ]
    for observed, corner in zip(list(polygon.exterior.coords)[:-1], expected, strict=True):
        assert observed == pytest.approx(corner, abs=1e-9)
    assert polygon.area == pytest.approx(36)
    assert record["elevation"] == pytest.approx(35)
    assert record["effective_layer"] == "Site"
    assert record["source"]["insert_path"] == [root.dxf.handle, child.dxf.handle]
    assert record["source"]["entity_handle"] == outline.dxf.handle
    assert record["source"]["root_handle"] == root.dxf.handle
    assert record["width_native"]["constant"] == .2
    assert record["width_wcs_max"] is None  # Nonuniform scales do not imply one WCS width.
    assert result["texts"][0]["text"] == "测试7#"
    assert result["texts"][0]["elevation"] == pytest.approx(35)


def test_masks_do_not_erase_geometry_and_unresolved_boundaries_are_identified(tmp_path):
    doc = ezdxf.new("R2018")
    model = doc.modelspace()
    outline = model.add_lwpolyline(
        [(0, 0), (3, 0), (3, 2), (0, 2)], close=True,
        dxfattribs={"extrusion": (0, 0, -1), "const_width": .1},
    )
    curved = model.add_lwpolyline(
        [(0, 0, 0, 0, 1), (3, 0, 0, 0, 0), (3, 2, 0, 0, 0)], format="xyseb", close=True,
    )
    crossed = model.add_lwpolyline([(0, 0), (1, 1), (0, 1), (1, 0)], close=True)
    tilted = model.add_lwpolyline(
        [(0, 0), (3, 0), (3, 2), (0, 2)], close=True, dxfattribs={"extrusion": (1, 1, 1)},
    )
    model.add_wipeout([(-4, -1), (1, -1), (1, 3), (-4, 3)])
    path = tmp_path / "limitations.dxf"
    doc.saveas(path)
    result = extract_drawing(path)

    assert len(result["polygons"]) == len(result["wipeouts"]) == 1
    reference = result["polygons"][0]
    assert reference["source"]["entity_handle"] == outline.dxf.handle
    assert shape(reference["geometry"]).bounds == (-3, 0, 0, 2)
    assert reference["width_wcs_max"] == pytest.approx(.1)
    assert reference["interpretation_status"] == "pending_semantic_and_width_confirmation"
    assert {issue["source"]["entity_handle"] for issue in result["issues"]} == {
        curved.dxf.handle, crossed.dxf.handle, tilted.dxf.handle,
    }
    assert all(issue["code"] == "boundary_not_resolved" for issue in result["issues"])


def test_array_occurrences_are_distinct_and_expansion_limit_does_not_return_partial_success(tmp_path):
    doc = ezdxf.new("R2018")
    tile = doc.blocks.new("Tile")
    source = tile.add_lwpolyline([(0, 0), (2, 0), (2, 1), (0, 1)], close=True)
    root = doc.modelspace().add_blockref(
        "Tile", (10, 20),
        dxfattribs={"column_count": 2, "row_count": 2, "column_spacing": 5, "row_spacing": 3},
    )
    path = tmp_path / "array.dxf"
    doc.saveas(path)
    result = extract_drawing(path)

    assert len(result["polygons"]) == len({p["id"] for p in result["polygons"]}) == 4
    assert {tuple(shape(p["geometry"]).bounds) for p in result["polygons"]} == {
        (10, 20, 12, 21), (15, 20, 17, 21), (10, 23, 12, 24), (15, 23, 17, 24),
    }
    assert {p["source"]["entity_handle"] for p in result["polygons"]} == {source.dxf.handle}
    assert {p["source"]["root_handle"] for p in result["polygons"]} == {root.dxf.handle}
    with pytest.raises(RuntimeError, match="no partial result"):
        extract_drawing(path, max_entities=2)


def test_unverified_xref_scope_propagates_to_nested_geometry_and_text(tmp_path):
    doc = ezdxf.new("R2018")
    nested = doc.blocks.new("Nested")
    nested.add_lwpolyline([(0, 0), (4, 0), (4, 2), (0, 2)], close=True)
    nested.add_text("7#", dxfattribs={"height": 1, "insert": (1, 1)})
    external = doc.blocks.new("CachedXref")
    external.block.dxf.flags = 4  # External-reference definition with cached contents.
    external.block.dxf.xref_path = "unverified-project.dxf"
    external.add_blockref("Nested", (10, 5))
    doc.modelspace().add_blockref("CachedXref", (100, 200))
    path = tmp_path / "xref.dxf"
    doc.saveas(path)
    result = extract_drawing(path)

    assert len(result["polygons"]) == len(result["texts"]) == 1
    # Retain reviewable cached coordinates, but never promote them to verified input.
    assert shape(result["polygons"][0]["geometry"]).bounds == (110, 205, 114, 207)
    for record in result["polygons"] + result["texts"] + result["inserts"]:
        assert record["geometry_status"] == "unresolved_external_reference"
        assert record["visibility"]["unverified_external_reference"] is True
    assert any(issue["code"] == "external_reference" for issue in result["issues"])
