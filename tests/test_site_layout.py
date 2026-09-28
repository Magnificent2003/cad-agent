from __future__ import annotations

import hashlib
import json
import math

import ezdxf
import pytest
from shapely import affinity
from shapely.geometry import Polygon

from cad_domain.geometry import check_dxf, entity_polygon
from cad_domain.layout import APPID, build_layout


def make_site(tmp_path, *, width=30, depth=20, metres_per_unit=1, angle=0,
              origin=(0, 0), obstacle=None):
    doc = ezdxf.new("R2018")
    doc.units = 6 if metres_per_unit == 1 else 4

    def world(points):
        polygon = Polygon(points)
        polygon = affinity.scale(polygon, xfact=1 / metres_per_unit,
                                 yfact=1 / metres_per_unit, origin=(0, 0))
        polygon = affinity.rotate(polygon, angle, origin=(0, 0))
        return affinity.translate(polygon, xoff=origin[0], yoff=origin[1])

    model = doc.modelspace()
    boundary = model.add_lwpolyline(list(world([(0, 0), (width, 0), (width, depth), (0, depth)]).exterior.coords)[:-1],
                                    close=True)
    obstacle_handles = []
    if obstacle:
        entity = model.add_lwpolyline(list(world(obstacle).exterior.coords)[:-1], close=True)
        obstacle_handles.append(entity.dxf.handle)
    original_text = model.add_text("原始标注", dxfattribs={"insert": (origin[0], origin[1]), "height": 1})
    path = tmp_path / "baseline.dxf"
    doc.saveas(path)
    facts = {
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "units": {"status": "confirmed", "metres_per_unit": metres_per_unit,
                  "evidence": "User confirmed the drawing units"},
        "boundary": {"status": "confirmed", "handles": [boundary.dxf.handle],
                     "evidence": "User confirmed the closed parcel boundary"},
        "obstacles": {"status": "confirmed", "handles": obstacle_handles,
                      "evidence": "User reviewed all existing footprints"},
        "frame": {"origin": list(origin), "angle_deg": angle},
    }
    return path, facts, (boundary.dxf.handle, original_text.dxf.handle)


def rectangle(component_id="A", *, position=None, width=4, depth=3, **extra):
    spec = {"id": component_id, "kind": "rectangle", "width_m": width, "depth_m": depth, **extra}
    if position is not None:
        spec["position_m"] = position
    return spec


def test_rotated_frame_and_native_units_preserve_original(tmp_path):
    source, facts, original_handles = make_site(tmp_path, metres_per_unit=0.001,
                                                angle=31, origin=(500_000, 4_000_000))
    before = source.read_bytes()
    target = tmp_path / "placed.dxf"
    result = build_layout(source, target, facts=facts,
                          components=[rectangle(position=[4, 5], angle_deg=12)])
    assert result["status"] == "passed", result
    assert source.read_bytes() == before
    original, output = ezdxf.readfile(source), ezdxf.readfile(target)
    for handle in original_handles:
        assert output.entitydb[handle].dxfattribs() == original.entitydb[handle].dxfattribs()
    entity = output.entitydb[result["components"][0]["handle"]]
    points = list(entity.get_points("xy"))
    assert math.dist(points[0], points[1]) == pytest.approx(4_000)
    assert math.dist(points[1], points[2]) == pytest.approx(3_000)
    angle = math.degrees(math.atan2(points[1][1] - points[0][1], points[1][0] - points[0][0]))
    assert angle == pytest.approx(43)
    label = output.entitydb[result["components"][0]["label_handle"]]
    assert label.dxf.rotation == pytest.approx(43)
    checked = check_dxf(source, target, profile="site_layout", facts=facts,
                        components=result["components"], allowed_changes=result["allowed_changes"])
    assert checked["status"] == "passed", checked


def test_consecutive_auto_adds_rebuild_occupancy_and_keep_dimensions(tmp_path):
    source, facts, _ = make_site(tmp_path, obstacle=[(0, 0), (6, 0), (6, 8), (0, 8)])
    facts["clearance_m"] = 0.5
    first = tmp_path / "first.dxf"
    added = build_layout(source, first, facts=facts, components=[rectangle("A")])
    assert added["status"] == "passed", added
    second = tmp_path / "second.dxf"
    added_again = build_layout(first, second, baseline=source, facts=facts, components=[rectangle("B")])
    assert added_again["status"] == "passed", added_again
    assert {c["id"] for c in added_again["components"]} == {"A", "B"}
    doc = ezdxf.readfile(second)
    a, b = [entity_polygon(doc, c["handle"]) for c in added_again["components"]]
    assert a.disjoint(b)
    obstacle = entity_polygon(doc, facts["obstacles"]["handles"][0])
    assert a.disjoint(obstacle) and b.disjoint(obstacle)
    assert a.area == pytest.approx(12) and b.area == pytest.approx(12)


def test_move_releases_previous_area_and_blocks_new_area(tmp_path):
    source, facts, _ = make_site(tmp_path)
    first, second, third = (tmp_path / name for name in ("first.dxf", "second.dxf", "third.dxf"))
    result = build_layout(source, first, facts=facts, components=[rectangle(position=[1, 1])])
    assert result["status"] == "passed", result
    old = result["components"][0]
    result = build_layout(first, second, baseline=source, facts=facts,
                          components=[{"id": "A", "operation": "move", "position_m": [12, 12]},
                                      rectangle("B", position=[1, 1])])
    assert result["status"] == "passed", result
    assert set(result["allowed_changes"]) == {old["handle"], old["label_handle"]}
    assert ezdxf.readfile(second).entitydb.get(old["handle"]) is None
    collision = build_layout(second, third, baseline=source, facts=facts,
                             components=[rectangle("C", position=[12, 12])])
    assert collision["status"] == "failed"
    assert collision["issues"][0]["code"] == "placement_conflict"
    assert not third.exists()


def test_remove_frees_occupancy_and_removes_label(tmp_path):
    source, facts, _ = make_site(tmp_path)
    first, second = tmp_path / "first.dxf", tmp_path / "second.dxf"
    result = build_layout(source, first, facts=facts, components=[rectangle(position=[1, 1])])
    assert result["status"] == "passed", result
    old = result["components"][0]
    result = build_layout(first, second, baseline=source, facts=facts,
                          components=[{"id": "A", "operation": "remove"}, rectangle("B", position=[1, 1])])
    assert result["status"] == "passed", result
    assert [c["id"] for c in result["components"]] == ["B"]
    doc = ezdxf.readfile(second)
    assert doc.entitydb.get(old["handle"]) is None
    assert doc.entitydb.get(old["label_handle"]) is None


def test_exact_conflict_rejects_entire_batch_without_publishing(tmp_path):
    source, facts, _ = make_site(tmp_path, obstacle=[(10, 10), (14, 10), (14, 14), (10, 14)])
    target = tmp_path / "target.dxf"
    target.write_bytes(b"existing target is retained")
    result = build_layout(source, target, facts=facts,
                          components=[rectangle("A", position=[1, 1]), rectangle("B", position=[11, 11])])
    assert result["status"] == "failed", result
    assert result["components"] == []
    assert target.read_bytes() == b"existing target is retained"
    assert list(tmp_path.glob(".target.dxf.*")) == []


def test_small_boundary_never_relaxes_dimensions(tmp_path):
    source, facts, _ = make_site(tmp_path, width=3, depth=3)
    target = tmp_path / "target.dxf"
    result = build_layout(source, target, facts=facts, components=[rectangle(width=4, depth=2)])
    assert result["status"] == "failed", result
    assert result["issues"][0]["code"] == "no_placement"
    assert "dimensions unchanged" in result["issues"][0]["message"]
    assert not target.exists()


def test_candidate_search_limit_is_explicit_failure(tmp_path):
    source, facts, _ = make_site(tmp_path, obstacle=[(0, 0), (10, 0), (10, 10), (0, 10)])
    facts["search"] = {"step_m": 1, "max_candidates": 1}
    result = build_layout(source, tmp_path / "target.dxf", facts=facts, components=[rectangle()])
    assert result["status"] == "failed", result
    assert result["issues"][0]["code"] == "candidate_limit"


def test_road_has_real_width_and_participates_in_occupancy(tmp_path):
    source, facts, _ = make_site(tmp_path, metres_per_unit=0.001)
    first, second = tmp_path / "first.dxf", tmp_path / "second.dxf"
    road = {"id": "road", "kind": "road", "width_m": 4, "points_m": [[2, 5], [25, 5]]}
    result = build_layout(source, first, facts=facts, components=[road])
    assert result["status"] == "passed", result
    doc = ezdxf.readfile(first)
    checked = check_dxf(source, first, profile="site_layout", facts=facts, components=result["components"])
    assert checked["status"] == "passed", checked
    assert doc.entitydb[result["components"][0]["handle"]].dxf.const_width == pytest.approx(4000)
    result = build_layout(first, second, baseline=source, facts=facts,
                          components=[rectangle(position=[10, 6])])
    assert result["status"] == "failed", result
    assert result["issues"][0]["code"] == "placement_conflict"
    assert not second.exists()


@pytest.mark.parametrize("points,width", [([[1, 1], [10, 10], [1, 10], [10, 1]], 1),
                                          ([[1, 1], [1, 1]], 1), ([[1, 1], [10, 1]], 0),
                                          ([[1, 0.5], [10, 0.5]], 2)])
def test_invalid_or_outside_road_is_rejected(tmp_path, points, width):
    source, facts, _ = make_site(tmp_path)
    target = tmp_path / "target.dxf"
    result = build_layout(source, target, facts=facts,
                          components=[{"id": "road", "kind": "road", "width_m": width, "points_m": points}])
    assert result["status"] == "failed", result
    assert not target.exists()


def test_long_names_use_short_labels_and_outside_legend(tmp_path):
    source, facts, _ = make_site(tmp_path)
    target = tmp_path / "target.dxf"
    name = "A facility with an intentionally long but single-line descriptive name"
    result = build_layout(source, target, facts=facts,
                          components=[rectangle("A", position=[1, 1], name=name)])
    assert result["status"] == "passed", result
    component = result["components"][0]
    doc = ezdxf.readfile(target)
    assert doc.entitydb[component["label_handle"]].dxf.text == "A"
    assert component["legend_handles"]
    boundary = entity_polygon(doc, facts["boundary"]["handles"][0])
    for handle in component["legend_handles"]:
        legend = doc.entitydb[handle]
        assert legend.dxf.insert.x > boundary.bounds[2]
        assert len(legend.dxf.text) <= 48
    tag = json.loads("".join(t.value for t in doc.entitydb[component["handle"]].get_xdata(APPID) if t.code == 1000))
    assert tag["name"] == name


def test_wrong_baseline_or_source_overwrite_does_not_publish(tmp_path):
    source, facts, _ = make_site(tmp_path)
    contents = source.read_bytes()
    result = build_layout(source, source, facts=facts, components=[rectangle()])
    assert result["status"] == "failed", result
    assert source.read_bytes() == contents
    facts["source_sha256"] = "0" * 64
    target = tmp_path / "target.dxf"
    result = build_layout(source, target, facts=facts, components=[rectangle()])
    assert result["status"] != "passed"
    assert not target.exists()


def test_changed_units_in_revised_source_are_rejected(tmp_path):
    source, facts, _ = make_site(tmp_path)
    revised = tmp_path / "revised.dxf"
    doc = ezdxf.readfile(source)
    doc.units = 4
    doc.saveas(revised)
    target = tmp_path / "target.dxf"
    result = build_layout(revised, target, baseline=source, facts=facts, components=[rectangle()])
    assert result["status"] == "failed", result
    assert result["issues"][0]["code"] == "units_changed"
    assert not target.exists()


def test_rotated_revised_component_pose_is_reloaded_for_move(tmp_path):
    source, facts, _ = make_site(tmp_path, metres_per_unit=0.001, angle=31, origin=(500_000, 4_000_000))
    first, second = tmp_path / "first.dxf", tmp_path / "second.dxf"
    result = build_layout(source, first, facts=facts,
                          components=[rectangle(position=[4, 5], angle_deg=12)])
    assert result["status"] == "passed", result
    result = build_layout(first, second, baseline=source, facts=facts,
                          components=[{"id": "A", "operation": "move", "position_m": [12, 10]}])
    assert result["status"] == "passed", result
    doc = ezdxf.readfile(second)
    component = doc.entitydb[result["components"][0]["handle"]]
    points = list(component.get_points("xy"))
    angle = math.degrees(math.atan2(points[1][1] - points[0][1], points[1][0] - points[0][0]))
    assert angle == pytest.approx(43)
    assert math.dist(points[0], points[1]) == pytest.approx(4000)
    assert math.dist(points[1], points[2]) == pytest.approx(3000)
