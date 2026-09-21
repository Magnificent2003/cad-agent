from __future__ import annotations

import hashlib
import ezdxf
import pytest

from backend.app.cad import CadError, edit_drawing, import_drawing, query_drawing, render_drawing
from backend.app.cad.constraints import check_drawing
from backend.app.cad.reader import snapshots


@pytest.fixture
def drawing(tmp_path):
    doc = ezdxf.new("R2018")
    doc.units = 4
    doc.layers.new("SITE")
    doc.layers.new("LOCKED").lock()
    model = doc.modelspace()
    site = model.add_lwpolyline([(0, 0), (100, 0), (100, 100), (0, 100)], close=True, dxfattribs={"layer": "SITE"})
    footprint = model.add_lwpolyline([(10, 10), (20, 10), (20, 20), (10, 20)], close=True)
    text = model.add_text("原始文字", dxfattribs={"height": 2, "insert": (12, 30)})
    mtext = model.add_mtext("first\\Psecond", dxfattribs={"char_height": 2, "insert": (40, 40)})
    line = model.add_line((0, 0), (5, 5))
    locked = model.add_line((1, 0), (5, 9), dxfattribs={"layer": "LOCKED"})
    block = doc.blocks.new("Room")
    block.add_line((0, 0), (4, 4))
    reference = model.add_blockref("Room", (70, 70))
    reference.add_attrib("CODE", "办公室", (70, 70))
    paper = doc.layouts.get("Layout1")
    paper.add_text("纸空间", dxfattribs={"height": 2})
    path = tmp_path / "original.dxf"
    doc.saveas(path)
    return path, {"site": site.dxf.handle, "footprint": footprint.dxf.handle, "text": text.dxf.handle, "mtext": mtext.dxf.handle, "line": line.dxf.handle, "locked": locked.dxf.handle, "insert": reference.dxf.handle, "block_line": block[0].dxf.handle}


def test_import_preview_and_read_only_source(drawing, tmp_path):
    source, handles = drawing
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    report = import_drawing(str(source), str(tmp_path / "r1"), {})
    assert report["units"] == {"code": 4, "name": "mm", "confirmed": True}
    assert {entry["name"] for entry in report["layouts"]} == {"Model", "Layout1"}
    assert report["entity_counts"]["TEXT"] == 2
    for name in report["artifacts"].values():
        assert (tmp_path / "r1" / name).stat().st_size > 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    assert report["audit"]["repair_applied"] is False
    preview = render_drawing(str(source), str(tmp_path / "r1"), "Layout1", settings={})
    assert preview["artifacts"]["png"] != "preview.png"
    region = render_drawing(str(source), str(tmp_path / "r1"), bbox=[10, 10, 20, 20])
    assert region["bbox"] == [10, 10, 20, 20]


def test_query_native_entities_layout_attributes_and_pagination(drawing):
    source, handles = drawing
    found = query_drawing(str(source), {"text": "办公室"})
    assert found["total"] == 1
    reference = found["entities"][0]
    assert reference["handle"] == handles["insert"]
    assert reference["layout"] == "Model"
    assert reference["attributes"][0]["parent_handle"] == handles["insert"]
    assert reference["block_entities_expanded"] is False
    assert len(reference["transform"]) == 16
    paper = query_drawing(str(source), {"layout": "Layout1", "types": ["TEXT"]})
    assert paper["entities"][0]["text"] == "纸空间"
    page = query_drawing(str(source), {"limit": 2, "offset": 1})
    assert len(page["entities"]) == 2 and page["total"] == 7
    bounded = query_drawing(str(source), {"types": ["LWPOLYLINE"], "bbox": [12, 12, 18, 18]})
    assert bounded["total"] == 2


def test_batch_edit_roundtrip_preserves_non_targets(drawing, tmp_path):
    source, handles = drawing
    original = snapshots(ezdxf.readfile(source))
    report = edit_drawing(str(source), str(tmp_path / "r2"), {"operations": [
        {"type": "set_text", "handles": [handles["text"]], "text": "更新标签"},
        {"type": "set_text", "handles": [handles["mtext"]], "text": "new\\Pcontent"},
        {"type": "translate_entities", "handles": [handles["footprint"]], "dx": 5, "dy": 3, "unit": "mm"},
        {"type": "add_rectangle", "origin": [30, 30], "width": 10, "height": 5, "unit": "mm"},
        {"type": "add_polyline", "points": [[0, 0], [2, 3]], "unit": "mm"},
        {"type": "add_text", "position": [30, 40], "height": 2, "text": "新增", "unit": "mm"},
    ]}, {})
    result = ezdxf.readfile(tmp_path / "r2/drawing.dxf")
    assert result.entitydb[handles["text"]].dxf.text == "更新标签"
    assert result.entitydb[handles["mtext"]].plain_text() == "new\ncontent"
    assert tuple(result.entitydb[handles["footprint"]].get_points()[0])[:2] == (15, 13)
    after = snapshots(result)
    assert report["changes"]["preservation_verified"] is True
    assert len(report["changes"]["added_handles"]) == 3
    for key in ("site", "line", "locked", "insert", "block_line"):
        assert after[handles[key]] == original[handles[key]]
    assert snapshots(ezdxf.readfile(source)) == original


@pytest.mark.parametrize("operation,code", [
    ({"type": "translate_entities", "handles": ["line"], "dx": 1, "unit": "m"}, "unit_mismatch"),
    ({"type": "translate_entities", "handles": ["line"], "dx": float("nan"), "unit": "mm"}, "invalid_operation"),
    ({"type": "set_text", "handles": ["line"], "text": "wrong"}, "invalid_targets"),
    ({"type": "set_text", "handles": ["mtext"], "text": "new", "preserve_mtext_format": True}, "unsupported_format_preservation"),
    ({"type": "translate_entities", "handles": ["locked"], "dx": 1, "unit": "mm"}, "locked_layer"),
    ({"type": "translate_entities", "handles": ["block_line"], "dx": 1, "unit": "mm"}, "invalid_targets"),
])
def test_invalid_edits_leave_original_intact(drawing, tmp_path, operation, code):
    source, handles = drawing
    operation["handles"] = [handles[name] for name in operation["handles"]]
    before = source.read_bytes()
    with pytest.raises(CadError) as error:
        edit_drawing(str(source), str(tmp_path / "rejected"), operation, {})
    assert error.value.code == code
    assert source.read_bytes() == before
    assert not (tmp_path / "rejected" / "drawing.dxf").exists()


def test_unknown_units_require_explicit_confirmation(drawing, tmp_path):
    source, handles = drawing
    doc = ezdxf.readfile(source)
    doc.units = 0
    doc.saveas(source)
    operation = {"type": "translate_entities", "handles": [handles["line"]], "dx": 1, "unit": "mm"}
    with pytest.raises(CadError, match="unspecified"):
        edit_drawing(str(source), str(tmp_path / "failed"), operation, {})
    report = edit_drawing(str(source), str(tmp_path / "confirmed"), {"operations": [operation], "confirmed_unit": "mm"}, {})
    assert report["units"]["code"] == 4
    assert ezdxf.readfile(source).units == 0


def test_geometry_pass_fail_unknown_and_applicability(drawing):
    source, handles = drawing
    constraints = [
        {"constraint_id": "inside", "targets": [handles["footprint"]], "predicate": "within_site", "parameters": {"site_handle": handles["site"]}, "unit": "mm", "source_kind": "demo_rule"},
        {"constraint_id": "overlap", "targets": [handles["footprint"], handles["site"]], "predicate": "no_overlap", "unit": "mm"},
        {"constraint_id": "missing-unit", "targets": [handles["footprint"]], "predicate": "within_site"},
        {"constraint_id": "not-applicable", "targets": [], "predicate": "no_overlap", "applies_if": False},
        {"constraint_id": "distance", "targets": [handles["line"], handles["footprint"]], "predicate": "min_distance", "parameters": {"minimum": 5}, "unit": "mm"},
        {"constraint_id": "no-threshold", "targets": [handles["line"], handles["footprint"]], "predicate": "min_distance", "unit": "mm"},
        {"constraint_id": "missing-width", "targets": [handles["line"]], "predicate": "within_site", "parameters": {"site_handle": handles["site"]}, "unit": "mm"},
    ]
    result = check_drawing(str(source), constraints, "r1")
    assert [check["status"] for check in result["checks"]] == ["pass", "fail", "unknown", "not_applicable", "pass", "unknown", "unknown"]
    assert result["checks"][1]["measured_value"]["maximum_overlap_area"] == 100
    assert result["rag_status"] == "not_configured"
    assert result["regulatory_review"] == "not_run"


def test_dwg_missing_converter_is_actionable(tmp_path, monkeypatch):
    monkeypatch.delenv("ODA_EXECUTABLE", raising=False)
    source = tmp_path / "source.dwg"
    source.write_bytes(b"AC1032 fake DWG")
    with pytest.raises(CadError) as error:
        import_drawing(str(source), str(tmp_path / "output"), {})
    assert error.value.code == "oda_unavailable"


def test_converter_timeout_kills_task_and_preserves_input(tmp_path):
    from backend.app.cad.converter import convert_file
    executable = tmp_path / "fake-oda"
    executable.write_text("#!/bin/sh\nsleep 10\n")
    executable.chmod(0o755)
    source, target = tmp_path / "source.dwg", tmp_path / "output.dxf"
    source.write_bytes(b"immutable")
    with pytest.raises(CadError) as error:
        convert_file(source, target, {"oda_executable": str(executable), "conversion_timeout": 1})
    assert error.value.code == "conversion_timeout"
    assert source.read_bytes() == b"immutable"
    assert not target.exists()


def test_non_target_mutation_is_rejected(drawing, tmp_path, monkeypatch):
    from ezdxf.entities import Line
    source, handles = drawing
    original_translate = Line.translate
    def faulty_translate(entity, *delta):
        result = original_translate(entity, *delta)
        entity.doc.entitydb[handles["site"]].dxf.layer = "0"
        return result
    monkeypatch.setattr(Line, "translate", faulty_translate)
    with pytest.raises(CadError) as error:
        edit_drawing(str(source), str(tmp_path / "bad"), {"type": "translate_entities", "handles": [handles["line"]], "dx": 1, "unit": "mm"}, {})
    assert error.value.code == "preservation_failed"
    assert ezdxf.readfile(source).entitydb[handles["site"]].dxf.layer == "SITE"


def test_move_insert_preserves_definition_and_moves_attributes(drawing, tmp_path):
    source, handles = drawing
    original = ezdxf.readfile(source)
    result = edit_drawing(str(source), str(tmp_path / "insert-moved"), {"type": "translate_entities", "handles": [handles["insert"]], "dx": 5, "dy": 6, "unit": "mm"}, {})
    changed = ezdxf.readfile(tmp_path / "insert-moved/drawing.dxf")
    reference = changed.entitydb[handles["insert"]]
    assert tuple(reference.dxf.insert) == (75, 76, 0)
    assert tuple(reference.attribs[0].dxf.insert) == (75, 76, 0)
    assert snapshots(original)[handles["block_line"]] == snapshots(changed)[handles["block_line"]]
    assert result["changes"]["preservation_verified"]


def test_coordinate_overflow_is_rejected(drawing, tmp_path):
    source, _ = drawing
    with pytest.raises(CadError) as error:
        edit_drawing(str(source), str(tmp_path / "overflow"), {"type": "add_rectangle", "origin": [1e308, 0], "width": 1e308, "height": 1, "unit": "mm"}, {})
    assert error.value.code == "invalid_operation"


def test_worker_termination_reaps_converter_process_group(tmp_path):
    import multiprocessing
    import os
    import time
    from backend.app.cad.converter import convert_file
    executable = tmp_path / "fake-oda"
    executable.write_text('#!/bin/sh\nprintf "%s" "$$" > "$1/../converter.pid"\nsleep 30\n')
    executable.chmod(0o755)
    source = tmp_path / "input.dwg"
    source.write_bytes(b"immutable")
    worker = multiprocessing.get_context("fork").Process(target=convert_file, args=(source, tmp_path / "output.dxf", {"oda_executable": str(executable), "conversion_timeout": 60}))
    worker.start()
    try:
        deadline = time.monotonic() + 5
        markers = []
        while time.monotonic() < deadline:
            markers = list(tmp_path.glob("oda-*/converter.pid"))
            if markers and markers[0].read_text():
                break
            time.sleep(0.02)
        assert markers
        converter_pid = int(markers[0].read_text())
        worker.terminate()
        worker.join(timeout=5)
        assert not worker.is_alive()
        with pytest.raises(ProcessLookupError):
            os.kill(converter_pid, 0)
        assert source.read_bytes() == b"immutable"
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=5)
