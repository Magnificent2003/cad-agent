"""Read-only regression of one existing historical delivery; no model/server.

Run from the repository: .venv/bin/python dev_log/round1_validation.py
Only explicit observed handles from this historical artifact are registered here.
These case-specific selections are not product inference rules. Site facts remain
unconfirmed; no boundary, obstacle inventory, or metres-per-unit is fabricated.
No geometry or reports are copied to disk; JSON is printed to stdout.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys

import ezdxf
from shapely.geometry import LineString

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cad_domain.geometry import check_dxf, digest  # noqa: E402

TASK = ROOT / "data/agent-tasks/786b4ced67ef4d1198bfe8bdf0359aee"
SOURCE = TASK / "input/drawing.dxf"
TARGET = TASK / "output/施工场地布置建议_总平面布置图.dxf"
# Handles and labels observed in the existing output DXF and its delivery notes.
FACILITIES = {
    "C908": "crane_1", "C90B": "crane_2", "C90E": "crane_3",
    "C911": "steel_processing", "C913": "formwork_processing",
    "C915": "steel_storage", "C917": "masonry_storage",
    "C919": "turnover_storage", "C91B": "aggregate_storage",
    "C91D": "office_living", "C91F": "wash_sedimentation",
    "C921": "temporary_power", "C923": "fire_equipment", "C925": "waste_storage",
}


def main():
    before = {str(path): digest(path) for path in (SOURCE, TARGET)}
    original = ezdxf.readfile(SOURCE)
    current = ezdxf.readfile(TARGET)
    old_handles = {e.dxf.handle for e in original.modelspace()}
    components = [{"id": name, "role": "facility", "handle": handle} for handle, name in FACILITIES.items()]
    components.append({"id": "temporary_road", "role": "road", "handle": "C907"})
    components.extend({"id": f"coverage_{h}", "role": "coverage", "handle": h} for h in ("C909", "C90C", "C90F"))
    for item in components:
        entity = current.entitydb[item["handle"]]
        assert entity.dxftype() == "LWPOLYLINE" and item["handle"] not in old_handles
    road = current.entitydb["C907"]
    points = [(p.x, p.y) for p in road.vertices_in_wcs()]
    if road.closed and points[0] != points[-1]:
        points.append(points[0])
    report = check_dxf(SOURCE, TARGET, profile="site_layout", facts=None, components=components)
    collisions = [i for i in report["issues"] if i["code"] == "component_collision"]
    assert report["status"] == "failed", report
    assert len(collisions) == 8, collisions
    assert all(i["measurement_units"] == "drawing_units2" for i in collisions)
    assert {i["code"] for i in report["issues"]} >= {"unconfirmed_facts", "invalid_road", "road_zero_width"}
    assert not LineString(points).is_simple and road.dxf.get("const_width", 0) == 0
    assert before == {str(path): digest(path) for path in (SOURCE, TARGET)}, "Regression must leave both originals unchanged."
    print(json.dumps({
        "source": str(SOURCE.relative_to(ROOT)), "target": str(TARGET.relative_to(ROOT)),
        "source_sha256": report["source_sha256"], "file_sha256": report["file_sha256"],
        "status": report["status"], "checks_are_model_independent": True,
        "facts": None, "site_dependent_geometry": report["checks"]["site_dependent_geometry"],
        "preservation": report["checks"]["preservation"]["status"],
        "registered_facilities": len(FACILITIES), "facility_overlap_pairs": len(collisions),
        "road": {"handle": "C907", "closed": road.closed, "centerline_is_simple": LineString(points).is_simple,
                 "actual_constant_width_drawing_units": road.dxf.get("const_width", 0)},
        "issue_counts": dict(Counter(i["code"] for i in report["issues"])),
        "issues": report["issues"],
        "limitations": "No units, site boundary, obstacles, containment, metre dimensions, crane coverage, or construction-code compliance are confirmed by this historical regression.",
        "input_and_output_unchanged": True,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
