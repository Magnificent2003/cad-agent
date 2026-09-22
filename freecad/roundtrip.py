#!/usr/bin/env python3
"""Diagnose an unedited import/export through upstream FreeCAD MCP execute_code.

Inputs must be prepared copies in .runtime/freecad-agent/files/input/. Outputs
and raw reports stay in that experimental workspace. No CAD editing tool is
implemented here. A successful export is not proof of drawing fidelity.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from compare_dxf import changes, digest_file, report

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / ".runtime/freecad-agent"
INPUT = WORKSPACE / "files/input"
MARKER = "FREECAD_ROUNDTRIP_RESULT="

CODE = '''import FreeCAD as App
from collections import Counter
import importDXF
import json
from pathlib import Path
config = json.loads(CONFIG_JSON)
previous = set(App.listDocuments())
p = App.ParamGet("User parameter:BaseApp/Preferences/Mod/Draft")
old_preferences = {key: p.GetBool(key, key in {"dxfUseDraftVisGroups", "dxfShowDialog"})
                   for key in config["preferences"]}
old_import_mode = p.GetInt("DxfImportMode", 2)
Path(config["import_report"]).with_name("preferences.json").write_text(
    json.dumps({"before": old_preferences, "applied": config["preferences"],
                "import_mode_before": old_import_mode}, indent=2) + "\\n", encoding="utf-8")
try:
    for key, value in config["preferences"].items():
        p.SetBool(key, value)
    if Path(config["input"]).suffix.lower() == ".dwg":
        import importDWG
        importDWG.open(config["input"])
    else:
        importDXF.open(config["input"])
    created = set(App.listDocuments()) - previous
    doc = App.ActiveDocument
    if doc is None or doc.Name not in created:
        raise RuntimeError("Importer did not activate a newly loaded document")
    objects = list(doc.Objects)
    if not objects:
        raise RuntimeError("Importer produced an empty document")
    feedback = {
        "freecad_version": list(App.Version()),
        "document_name": doc.Name,
        "objects": len(objects),
        "object_types": dict(Counter(obj.TypeId for obj in objects)),
        "unit_schema_preference": App.ParamGet("User parameter:BaseApp/Preferences/Units").GetInt("UserSchema", 0),
        "one_mm_quantity": str(App.Units.Quantity("1 mm")),
        "source_dxf_units_code": config["source_dxf_units_code"],
        "outputs": config["outputs"],
        "preferences_applied": config["preferences"],
        "preferences_before": old_preferences,
        "import_mode_before": old_import_mode,
        "import_mode_applied": p.GetInt("DxfImportMode", 2),
    }
    Path(config["import_report"]).write_text(json.dumps(feedback, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    for kind, filename in config["outputs"].items():
        if Path(filename).exists():
            raise RuntimeError("Refusing to overwrite an existing output")
        if kind == "dxf":
            importDXF.export(objects, filename)
        elif kind == "dwg":
            import importDWG
            importDWG.export(objects, filename)
        elif kind == "fcstd":
            doc.saveAs(filename)
        if not Path(filename).is_file() or not Path(filename).stat().st_size:
            raise RuntimeError("Exporter did not produce a nonempty " + kind + " file")
    print("FREECAD_ROUNDTRIP_RESULT=" + json.dumps(feedback, ensure_ascii=False))
finally:
    try:
        for name in set(App.listDocuments()) - previous:
            App.closeDocument(name)
    finally:
        for key, value in old_preferences.items():
            p.SetBool(key, value)
        p.SetInt("DxfImportMode", old_import_mode)
'''


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def execute(code: str, timeout: float, response_path: Path):
    parameters = StdioServerParameters(command="bash", args=[str(ROOT / "freecad/mcp.sh")])
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=float(2 * timeout + 60)) as session:
            await session.initialize()
            result = await session.call_tool("execute_code", {
                "code": code, "include_screenshot": False, "timeout": timeout,
            })
    # Interpret ordinary tool failures after MCP's AnyIO contexts have closed,
    # so readable diagnostics are not wrapped in a TaskGroup ExceptionGroup.
    write_json(response_path, result.model_dump(mode="json"))
    texts = [block.text for block in result.content if block.type == "text"]
    feedback = None
    for text in texts:
        if MARKER in text:
            payload = text.split(MARKER, 1)[1].splitlines()[0]
            feedback = json.loads(payload)
    if result.is_error or feedback is None:
        detail = "\n".join(texts)[:1000]
        raise RuntimeError(f"MCP did not report completed import/export; inspect response.json. {detail}")
    return feedback


async def run(args) -> dict:
    source = (INPUT / args.drawing).resolve(strict=True)
    if not source.is_relative_to(INPUT.resolve()) or source.suffix.lower() not in {".dwg", ".dxf"}:
        raise ValueError("Input must be a prepared .dwg/.dxf file under the experimental input directory")
    run_id = f"{source.stem}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    output_dir = WORKSPACE / "files/output" / run_id
    report_dir = WORKSPACE / "reports" / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    report_dir.mkdir(parents=True, exist_ok=False)
    # Preserve the imported document before a possibly failing DXF/DWG export.
    outputs = {kind: str(output_dir / f"roundtrip.{kind}") for kind in [*(["fcstd"] if args.fcstd else []), "dxf", *(["dwg"] if args.dwg else [])]}
    source_hash = digest_file(source)
    baseline_path = source if source.suffix.lower() == ".dxf" else source.with_suffix(".dxf")
    baseline = report(baseline_path) if baseline_path.is_file() else None
    preferences = {"dxfUseLegacyImporter": args.legacy_import,
                   "dxfImportAsDraft": not args.legacy_import,
                   "dxfImportAsPrimitives": False, "dxfImportAsShapes": args.legacy_import,
                   "dxfImportAsFused": False, "dxfCreateSketch": False,
                   "dxfUseDraftVisGroups": not args.plain_groups,
                   "dxfShowDialog": False, "dxfUseLegacyExporter": True}
    config = {"input": str(source), "outputs": outputs, "preferences": preferences,
              "options": {"legacy_import": args.legacy_import, "plain_groups": args.plain_groups},
              "import_report": str(report_dir / "import-result.json"),
              "source_dxf_units_code": baseline["units_code"] if baseline else None}
    code = CODE.replace("CONFIG_JSON", repr(json.dumps(config)))
    (report_dir / "execute_code.py").write_text(code, encoding="utf-8")
    write_json(report_dir / "request.json", {"tool": "execute_code", "timeout": args.timeout,
               "include_screenshot": False, "source_sha256": source_hash, **config})
    result = {"completed": False, "source": str(source), "outputs": outputs, "reports": str(report_dir)}
    try:
        result["freecad"] = await execute(code, args.timeout, report_dir / "response.json")
        if any(not Path(path).is_file() or not Path(path).stat().st_size for path in outputs.values()):
            raise RuntimeError("A declared output is missing or empty")
        if baseline:
            exported = report(Path(outputs["dxf"]))
            write_json(report_dir / "dxf-comparison.json", {
                "before": baseline, "after": exported, "differences": changes(baseline, exported),
            })
        result["completed"] = True
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        import_report = report_dir / "import-result.json"
        if import_report.is_file():
            result["import_result"] = str(import_report)
        result["input_unchanged"] = digest_file(source) == source_hash
        if not result["input_unchanged"]:
            result["completed"] = False
        result["fidelity_verified"] = False
        write_json(report_dir / "summary.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("drawing", type=Path, help="Prepared filename, e.g. simple.dxf, or path within input/")
    parser.add_argument("--dwg", action="store_true", help="Also export DWG using FreeCAD's configured converter")
    parser.add_argument("--fcstd", action="store_true", help="Save FreeCAD's native document before DXF/DWG export")
    parser.add_argument("--legacy-import", action="store_true", help="Use legacy importer with individual Part shapes; default: native importer with Draft objects")
    parser.add_argument("--plain-groups", action="store_true", help="Disable Draft layer groups; requires --legacy-import")
    parser.add_argument("--timeout", type=float, default=600, help="Per-call queue/run budget, 1–600 seconds")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error("--timeout must be between 1 and 600 seconds")
    if args.plain_groups and not args.legacy_import:
        parser.error("--plain-groups requires --legacy-import")
    try:
        result = asyncio.run(run(args))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Unable to start round trip: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["completed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
