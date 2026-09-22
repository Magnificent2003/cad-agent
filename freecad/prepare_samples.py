#!/usr/bin/env python3
"""Prepare disposable CAD inputs without changing their source files.

Examples:
    .venv/bin/python freecad/prepare_samples.py --synthetic
    .venv/bin/python freecad/prepare_samples.py --drawing /path/to/input.dwg --name sample.dwg

Files and the provenance manifest go to .runtime/freecad-agent/files/input/ by
default. They are local experimental data and must not be committed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".runtime" / "freecad-agent" / "files" / "input"


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def synthetic(path: Path) -> None:
    import ezdxf

    doc = ezdxf.new("R2013")
    doc.units = ezdxf.units.MM
    for name, color in [("FRAME", 3), ("CONTENT", 7), ("OUTSIDE", 1), ("CROSSING", 2)]:
        doc.layers.new(name, dxfattribs={"color": color})
    model = doc.modelspace()
    model.add_lwpolyline([(0, 0), (100, 0), (100, 60), (0, 60)], close=True,
                         dxfattribs={"layer": "FRAME"})
    model.add_text("ORIGINAL", dxfattribs={"layer": "CONTENT", "height": 4, "insert": (10, 15)})
    model.add_circle((30, 30), radius=5, dxfattribs={"layer": "CONTENT"})
    model.add_text("OUTSIDE", dxfattribs={"layer": "OUTSIDE", "height": 4, "insert": (120, 20)})
    model.add_line((120, 30), (145, 30), dxfattribs={"layer": "OUTSIDE"})
    model.add_line((90, 45), (110, 45), dxfattribs={"layer": "CROSSING"})
    doc.saveas(path)


def prepare(args) -> dict:
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"schema_version": 1, "files": {}}
    source = args.drawing.resolve(strict=True) if args.drawing else None
    name = args.name or ("simple.dxf" if args.synthetic else source.name)
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("--name must be a filename, not a directory path")
    if Path(name).suffix.lower() not in {".dxf", ".dwg"}:
        raise ValueError("Only .dxf and .dwg filenames are supported")
    if args.synthetic and Path(name).suffix.lower() != ".dxf":
        raise ValueError("Synthetic drawings must use a .dxf filename")
    if source and (not source.is_file() or source.suffix.lower() != Path(name).suffix.lower()):
        raise ValueError("Source must be a file and destination must keep its .dxf/.dwg suffix")
    destination = output / name
    if source == destination:
        raise ValueError("Source and destination must be different files")
    original_hash = digest_file(source) if source else None
    existing = manifest["files"].get(name)
    if destination.exists():
        current_hash = digest_file(destination)
        if not existing or current_hash != existing.get("copy_sha256"):
            raise ValueError(f"Refusing to replace an untracked or modified input: {destination}")
        if source and original_hash != current_hash:
            raise ValueError(f"Destination contains a different source: {destination}")
        if args.synthetic != (existing.get("source_kind") == "synthetic"):
            raise ValueError(f"Destination has a different source kind: {destination}")
        return {"path": str(destination), "sha256": current_hash, "already_prepared": True}

    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=output) as temporary:
        staged = Path(temporary) / name
        if args.synthetic:
            synthetic(staged)
        else:
            shutil.copy2(source, staged)
        copied_hash = digest_file(staged)
        if source and (original_hash != copied_hash or digest_file(source) != original_hash):
            raise ValueError("Source changed while being copied; no input was published")
        # Opening in exclusive mode prevents overwriting inputs created concurrently.
        with staged.open("rb") as reader, destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer)

    entry = {"source_kind": "synthetic" if args.synthetic else "copy",
             "prepared_at": datetime.now(timezone.utc).isoformat(),
             "source_path": str(source) if source else None,
             "source_sha256": original_hash, "copy_sha256": copied_hash,
             "file_bytes": destination.stat().st_size}
    manifest["files"][name] = entry
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output, prefix=".manifest-", delete=False) as writer:
        json.dump(manifest, writer, ensure_ascii=False, indent=2)
        writer.write("\n")
        staged_manifest = Path(writer.name)
    staged_manifest.replace(manifest_path)
    return {"path": str(destination), "sha256": copied_hash, "already_prepared": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--synthetic", action="store_true", help="Create a small 2D drawing for agent editing tests")
    modes.add_argument("--drawing", type=Path, help="Copy an existing DWG or DXF without editing the source")
    parser.add_argument("--name", help="Destination filename; defaults to source basename or simple.dxf")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        result = prepare(args)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Unable to prepare sample: {exc}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
