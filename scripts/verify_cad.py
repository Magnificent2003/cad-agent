"""Read/preview one local CAD file; optionally verify DWG export by reimport."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--out", required=True)
    parser.add_argument("--export-dwg", action="store_true")
    args = parser.parse_args()
    output = Path(args.out).resolve()
    if output.exists():
        raise SystemExit("请选择新的输出目录，避免覆盖已有验收结果。")
    from backend.app.config import Settings
    from backend.app.cad import import_drawing, export_drawing
    settings = Settings().cad_settings()
    start = time.monotonic()
    metadata = import_drawing(str(Path(args.source).resolve()), str(output), settings)
    report = {"duration_seconds": round(time.monotonic() - start, 3), "units": metadata["units"], "entity_counts": metadata["entity_counts"], "layouts": metadata["layouts"], "warnings": metadata["warnings"]}
    if args.export_dwg:
        report["dwg_export"] = export_drawing(str(output / "drawing.dxf"), str(output / "export"), "dwg", settings)
    (output / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
