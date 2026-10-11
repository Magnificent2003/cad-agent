"""Export a same-source PDF with the application's renderer, without editing CAD."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from matplotlib.figure import Figure  # noqa: E402
from backend.app.cad.renderer import render_drawing  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--context', type=Path, required=True)
    p.add_argument('--pdf', type=Path, required=True)
    p.add_argument('--scratch', type=Path, required=True)
    args = p.parse_args()
    before = hashlib.sha256(args.source.read_bytes()).hexdigest()
    context = json.loads(args.context.read_text())
    original_save = Figure.savefig
    args.pdf.parent.mkdir(parents=True, exist_ok=True)

    def save(figure, path, *a, **kw):
        result = original_save(figure, path, *a, **kw)
        if kw.get('format') == 'svg':
            original_save(figure, args.pdf, format='pdf', facecolor=figure.get_facecolor())
        return result

    Figure.savefig = save
    try:
        render_drawing(str(args.source), str(args.scratch), settings=context.get('review', {}).get('render_settings'))
    finally:
        Figure.savefig = original_save
    assert hashlib.sha256(args.source.read_bytes()).hexdigest() == before
    print(json.dumps({'source_sha256': before, 'pdf': str(args.pdf), 'source_unchanged': True}))


if __name__ == '__main__':
    main()
