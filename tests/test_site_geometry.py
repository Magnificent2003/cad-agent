from __future__ import annotations

import json

import ezdxf
import pytest
from shapely.geometry import Point

from cad_domain.geometry import check_dxf, digest, entity_polygon, inspect_dxf, load_site


def rectangle(space, x, y, w=10, h=10):
    return space.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True)


@pytest.fixture
def site(tmp_path):
    source = tmp_path / "source.dxf"
    target = tmp_path / "target.dxf"
    doc = ezdxf.new("R2013")
    doc.units = 6
    boundary = rectangle(doc.modelspace(), 0, 0, 500, 200)
    doc.saveas(source)
    facts = {
        "source_sha256": digest(source),
        "units": {"status": "confirmed", "metres_per_unit": 1, "evidence": "Survey dimensions confirmed in metres."},
        "boundary": {"status": "confirmed", "handles": [boundary.dxf.handle], "evidence": "Survey boundary selected explicitly."},
        "obstacles": {"status": "confirmed", "handles": [], "evidence": "Survey confirms this fixture is vacant."},
    }
    return source, target, facts


def component(entity, index="one", role="facility", **kw):
    return {"id": index, "role": role, "handle": entity.dxf.handle, **kw}


def codes(report):
    return {i["code"] for i in report["issues"]}


def test_general_roundtrip_preserves_original_and_allows_additions(site):
    source, target, _ = site
    doc = ezdxf.readfile(source)
    rectangle(doc.modelspace(), 20, 20)
    doc.saveas(target)
    report = check_dxf(source, target)
    assert report["status"] == "passed", report
    assert report["source_sha256"] == digest(source)
    assert report["file_sha256"] == digest(target)


def test_actual_file_detects_eight_overlap_pairs(site):
    source, target, facts = site
    doc = ezdxf.readfile(source)
    components = []
    for i in range(8):
        for j in range(2):
            entity = rectangle(doc.modelspace(), 10 + 50 * i + 5 * j, 10)
            components.append(component(entity, f"{i}-{j}"))
    doc.saveas(target)
    report = check_dxf(source, target, profile="site_layout", facts=facts, components=components)
    collisions = [i for i in report["issues"] if i["code"] == "component_collision"]
    assert report["status"] == "failed", report
    assert len(collisions) == 8
    assert all(i["location"] and i["measured"] == 50 for i in collisions)
    # Editing bytes after a previous result always triggers a fresh computation.
    old_hash = report["file_sha256"]
    for i, c in enumerate(components):
        doc.entitydb[c["handle"]].translate(0, 20 * (i % 2), 0)
    doc.saveas(target)
    updated = check_dxf(source, target, profile="site_layout", facts=facts, components=components)
    assert updated["status"] == "passed", updated
    assert updated["file_sha256"] != old_hash


def test_full_polygon_containment_rejects_concave_crossing(site):
    source, target, facts = site
    doc = ezdxf.readfile(source)
    boundary = doc.entitydb[facts["boundary"]["handles"][0]]
    boundary.set_points([(0, 0), (10, 0), (10, 10), (7, 10), (7, 3), (3, 3), (3, 10), (0, 10)])
    doc.saveas(source)
    facts["source_sha256"] = digest(source)
    site_geometry = load_site(source, facts).boundary
    crossing = rectangle(doc.modelspace(), 2, 6, 6, 2)
    assert all(site_geometry.covers(Point(x, y)) for x, y in crossing.get_points("xy"))
    doc.saveas(target)
    report = check_dxf(source, target, profile="site_layout", facts=facts, components=[component(crossing)])
    assert "outside_boundary" in codes(report)
    assert next(i for i in report["issues"] if i["code"] == "outside_boundary")["location"]


@pytest.mark.parametrize("points,width", [([(10, 10), (20, 20)], 0), ([(10, 10), (30, 30), (10, 30), (30, 10)], 2)])
def test_road_zero_width_and_self_intersection_fail(site, points, width):
    source, target, facts = site
    doc = ezdxf.readfile(source)
    road = doc.modelspace().add_lwpolyline(points, dxfattribs={"const_width": width})
    doc.saveas(target)
    report = check_dxf(source, target, profile="site_layout", facts=facts, components=[component(road, role="road", width_m=2)])
    assert report["status"] == "failed", report
    assert "invalid_road" in codes(report)


def test_road_true_width_and_corridor_are_checked(site):
    source, target, facts = site
    doc = ezdxf.readfile(source)
    road = doc.modelspace().add_lwpolyline([(1, 1), (50, 1)], dxfattribs={"const_width": 4})
    doc.saveas(target)
    report = check_dxf(source, target, profile="site_layout", facts=facts, components=[component(road, role="road", width_m=6)])
    assert {"road_width_unmet", "outside_boundary"} <= codes(report)


@pytest.mark.parametrize("mutation,expected", [("missing", "unconfirmed_facts"), ("unconfirmed", "unconfirmed_facts"), ("no_obstacles", "unconfirmed_facts"), ("empty_evidence", "unconfirmed_facts"), ("stale", "stale_facts")])
def test_unknown_or_stale_facts_never_pass(site, mutation, expected):
    source, target, facts = site
    doc = ezdxf.readfile(source)
    facility = rectangle(doc.modelspace(), 10, 10)
    doc.saveas(target)
    if mutation == "missing":
        facts = None
    elif mutation == "unconfirmed":
        facts["units"]["status"] = "declared"
    elif mutation == "no_obstacles":
        del facts["obstacles"]
    elif mutation == "empty_evidence":
        facts["obstacles"]["evidence"] = " "
    else:
        facts["source_sha256"] = "0" * 64
    report = check_dxf(source, target, profile="site_layout", facts=facts, components=[component(facility)])
    assert report["status"] != "passed"
    assert expected in codes(report)


def test_unit_header_requires_confirmed_override(site):
    source, _, facts = site
    facts["units"]["metres_per_unit"] = .6
    facts["units"]["evidence"] = "Paper plot scale 1:600 only."
    with pytest.raises(ValueError, match="override_header"):
        load_site(source, facts)
    facts["units"].update(override_header=True, evidence="Known survey segment measured 6 m over 10 native units, overriding header.")
    assert load_site(source, facts).metres_per_unit == .6
    declaration = inspect_dxf(source)["units"]
    assert declaration["status"] == "declared" and declaration["confirmed"] is False


@pytest.mark.parametrize("change", ["geometry", "attribute", "block_base", "layer", "units"])
def test_original_native_block_attributes_and_units_protected(site, change):
    source, target, _ = site
    doc = ezdxf.readfile(source)
    block = doc.blocks.new("Native")
    native = rectangle(block, 0, 0)
    insert = doc.modelspace().add_blockref("Native", (50, 50))
    attrib = insert.add_attrib("ID", "original", (50, 50))
    doc.saveas(source)
    if change == "geometry":
        native.translate(5, 0, 0)
    elif change == "attribute":
        attrib.dxf.text = "changed"
    elif change == "block_base":
        block.block.dxf.base_point = (1, 0, 0)
    elif change == "layer":
        doc.layers.get("0").dxf.color = 3
    else:
        doc.units = 4
    doc.saveas(target)
    report = check_dxf(source, target)
    assert report["status"] == "failed", report
    assert "units_changed" in codes(report) if change == "units" else "original_entity_changed" in codes(report)


def test_explicit_allowance_records_changes_but_cannot_change_site_reference(site):
    source, target, facts = site
    doc = ezdxf.readfile(source)
    handle = facts["boundary"]["handles"][0]
    doc.entitydb[handle].translate(1, 0, 0)
    facility = rectangle(doc.modelspace(), 10, 10)
    doc.saveas(target)
    general = check_dxf(source, target, allowed_changes=[handle])
    assert general["status"] == "passed", general
    assert general["checks"]["preservation"]["allowed_changed_handles"] == [handle]
    report = check_dxf(source, target, profile="site_layout", facts=facts, components=[component(facility)], allowed_changes=[handle])
    assert "site_reference_changed" in codes(report)


@pytest.mark.parametrize("kind", ["bulge", "circle", "multiple_insert", "tilted"])
def test_unsupported_footprint_does_not_become_bbox(site, kind):
    source, _, facts = site
    doc = ezdxf.readfile(source)
    if kind == "bulge":
        entity = doc.modelspace().add_lwpolyline([(0, 0, 1), (10, 0, 0), (10, 10, 0)], format="xyb", close=True)
    elif kind == "circle":
        entity = doc.modelspace().add_circle((20, 20), 10)
    elif kind == "multiple_insert":
        block = doc.blocks.new("Ambiguous")
        rectangle(block, 0, 0)
        rectangle(block, 20, 0)
        entity = doc.modelspace().add_blockref("Ambiguous", (0, 0))
    else:
        entity = rectangle(doc.modelspace(), 10, 10)
        entity.dxf.extrusion = (0, 1, 1)
    with pytest.raises(ValueError):
        entity_polygon(doc, entity.dxf.handle)
    doc.saveas(source)
    facts["source_sha256"] = digest(source)
    facts["boundary"]["handles"] = [entity.dxf.handle]
    with pytest.raises(ValueError):
        load_site(source, facts)
    assert inspect_dxf(source, [entity.dxf.handle])["entities"][0]["footprint"]["status"] == "unsupported"


def test_nested_insert_evidence_and_verified_ocs_transform(tmp_path):
    doc = ezdxf.new("R2013")
    inside = doc.blocks.new("Inside")
    rectangle(inside, 0, 0, 2, 1)
    outer = doc.blocks.new("Outer")
    outer.add_blockref("Inside", (10, 0))
    insert = doc.modelspace().add_blockref("Outer", (20, 30), dxfattribs={"rotation": 90})
    polygon = entity_polygon(doc, insert.dxf.handle)
    assert polygon.bounds == pytest.approx((19, 40, 20, 42))
    path = tmp_path / "nested.dxf"
    doc.saveas(path)
    record = inspect_dxf(path, [insert.dxf.handle])["entities"][0]
    assert record["children"][0]["children"][0]["footprint"]["area_drawing_units2"] == pytest.approx(2)
    assert record["children"][0]["block_path"] == ["Outer", "Inside"]
    reflected = rectangle(doc.modelspace(), 1, 1, 2, 1)
    reflected.dxf.extrusion = (0, 0, -1)
    assert entity_polygon(doc, reflected.dxf.handle).bounds == pytest.approx((-3, 1, -1, 2))


def test_xdata_recovery_and_unregistered_additions(site):
    source, target, facts = site
    doc = ezdxf.readfile(source)
    facility = rectangle(doc.modelspace(), 20, 20)
    doc.appids.add("CAD_AGENT_SITE")
    data = json.dumps(component(facility, width_m=10, depth_m=10))
    facility.set_xdata("CAD_AGENT_SITE", [(1000, data[:30]), (1000, data[30:])])
    doc.saveas(target)
    report = check_dxf(source, target, profile="site_layout", facts=facts)
    assert report["status"] == "passed", report
    rogue = rectangle(doc.modelspace(), 100, 100)
    doc.saveas(target)
    report = check_dxf(source, target, profile="site_layout", facts=facts)
    assert report["status"] == "unknown", report
    assert next(i for i in report["issues"] if i["code"] == "unregistered_spatial_entity")["handles"] == [rogue.dxf.handle]


def test_obstacles_clearance_and_facility_road_collision(site):
    source, target, facts = site
    doc = ezdxf.readfile(source)
    obstacle = rectangle(doc.modelspace(), 20, 20)
    doc.saveas(source)
    facts["source_sha256"] = digest(source)
    facts["obstacles"]["handles"] = [obstacle.dxf.handle]
    facts["clearance_m"] = 2
    facility = rectangle(doc.modelspace(), 31, 20)
    road = doc.modelspace().add_lwpolyline([(10, 25), (50, 25)], dxfattribs={"const_width": 4})
    doc.saveas(target)
    report = check_dxf(source, target, profile="site_layout", facts=facts, components=[component(facility), component(road, "road", "road")])
    assert {"obstacle_clearance_unmet", "obstacle_collision", "component_collision"} <= codes(report)


def test_intrinsic_failures_without_unconfirmed_site_assumptions(site):
    source, target, _ = site
    doc = ezdxf.readfile(source)
    components = []
    for i in range(8):
        for j in range(2):
            entity = rectangle(doc.modelspace(), 10 + 50 * i + 5 * j, 10)
            components.append(component(entity, f"{i}-{j}"))
    road = doc.modelspace().add_lwpolyline([(10, 100), (30, 100)])
    components.append(component(road, "road", "road", width_m=4))
    doc.saveas(target)
    report = check_dxf(source, target, profile="site_layout", components=components)
    overlaps = [i for i in report["issues"] if i["code"] == "component_collision"]
    assert len(overlaps) == 8
    assert all(i["measurement_units"] == "drawing_units2" for i in overlaps)
    assert {"unconfirmed_facts", "invalid_road"} <= codes(report)
    assert report["checks"]["site_dependent_geometry"] == "skipped_unconfirmed_facts"
    assert report["status"] == "failed"


def test_large_nested_inspection_has_global_budget_and_overview(tmp_path):
    doc = ezdxf.new("R2013")
    doc.layers.new("observed", dxfattribs={"color": 3})
    for i in range(100):
        doc.layers.new(f"{i}" + "长层名" * 55)
    block = doc.blocks.new("Large")
    for i in range(300):
        block.add_mtext("long-text-" * 500)
        rectangle(block, i, 0)
    outer = doc.blocks.new("Outer")
    outer.add_blockref("Large", (20, 20))
    ref = doc.modelspace().add_blockref("Outer", (10, 10), dxfattribs={"layer": "observed"})
    path = tmp_path / "large.dxf"
    doc.saveas(path)
    report = inspect_dxf(path, [ref.dxf.handle])
    assert report["truncated"]
    assert len(json.dumps(report, ensure_ascii=True)) <= 64000
    assert report["semantic_facts_inferred"] is False
    layer = next(item for item in report["overview"]["layers"] if item["name"] == "observed")
    assert layer["color"] == 3 and layer["entity_types"] == {"INSERT": 1}


def test_block_local_handle_cannot_masquerade_as_wcs_boundary(site):
    source, _, facts = site
    doc = ezdxf.readfile(source)
    block = doc.blocks.new("Building")
    local = rectangle(block, 0, 0)
    doc.modelspace().add_blockref("Building", (100, 100))
    doc.saveas(source)
    facts["source_sha256"] = digest(source)
    facts["boundary"]["handles"] = [local.dxf.handle]
    with pytest.raises(ValueError, match="modelspace"):
        load_site(source, facts)
    inspection = inspect_dxf(source, [local.dxf.handle])
    assert not inspection["candidate_handles"]
    assert inspection["entities"][0]["footprint"]["coordinate_system"] == "definition_or_paperspace_local"


def test_explicit_empty_registry_accepts_removing_last_component(site):
    from cad_domain.layout import build_layout

    source, first, facts = site
    added = build_layout(source, first, facts=facts, components=[
        {'id': 'A', 'kind': 'rectangle', 'width_m': 4, 'depth_m': 3, 'position_m': [10, 10]},
    ])
    assert added['status'] == 'passed', added
    empty = first.with_name('empty.dxf')
    removed = build_layout(first, empty, baseline=source, facts=facts,
                           components=[{'id': 'A', 'operation': 'remove'}])
    assert removed['status'] == 'passed' and removed['components'] == [], removed
    reviewed = check_dxf(source, empty, profile='site_layout', facts=facts,
                         components=removed['components'], allowed_changes=removed['allowed_changes'])
    assert reviewed['status'] == 'passed', reviewed
    # Omitted registry still means no component evidence has been supplied.
    omitted = check_dxf(source, empty, profile='site_layout', facts=facts)
    assert omitted['status'] == 'unknown' and 'no_spatial_components' in codes(omitted)


@pytest.mark.parametrize('kind', ['retained_tagged', 'new_untagged'])
def test_empty_registry_cannot_hide_remaining_spatial_components(site, kind):
    source, target, facts = site
    doc = ezdxf.readfile(source)
    facility = rectangle(doc.modelspace(), 10, 10)
    if kind == 'retained_tagged':
        doc.appids.add('CAD_AGENT_SITE')
        facility.set_xdata('CAD_AGENT_SITE', [(1000, json.dumps(component(facility)))])
        # The component already belongs to the original input: preservation
        # alone is insufficient to verify its current spatial constraints.
        doc.saveas(source)
        facts['source_sha256'] = digest(source)
    doc.saveas(target)
    reviewed = check_dxf(source, target, profile='site_layout', facts=facts, components=[])
    assert reviewed['status'] == 'unknown', reviewed
    expected = 'unregistered_spatial_component' if kind == 'retained_tagged' else 'unregistered_spatial_entity'
    assert expected in codes(reviewed)
