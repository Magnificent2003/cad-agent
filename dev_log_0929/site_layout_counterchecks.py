"""Reproduce three adversarial checks on copies of the actual Part B delivery.

Run from the project: .venv/bin/python dev_log_0929/site_layout_counterchecks.py
Only temporary DXF copies are changed; the baseline and delivery remain intact.
"""
import argparse
import shutil
import sys
import json
import math
from pathlib import Path
import tempfile

import ezdxf
from shapely.geometry import shape
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from cad_domain.geometry import _metadata, digest, occupied_polygon  # noqa: E402
from cad_domain.site_plan_review import review_layout  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "dev_log_0929/v0009_site_layout_report.json")
parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "dev_log_0929/v0009_site_layout_counterchecks.json")
args = parser.parse_args()
report_path = args.report
data = json.loads(report_path.read_text())
source, target = [Path(data[key]) if Path(data[key]).is_absolute() else PROJECT_ROOT / data[key] for key in ('source', 'target')]
scene, program = data['scene'], data['program']
source_hash, target_hash = digest(source), digest(target)
baseline = review_layout(source, target, scene, program)
if baseline["status"] != "passed":
    raise SystemExit("Delivered file failed independent review; do not run counterchecks as if it passed")
output = Path(tempfile.mkdtemp(prefix='cad_actual_layout_counterchecks_'))
summary = {'source_sha256':source_hash, 'delivery_sha256':target_hash, 'baseline_review':{'status':baseline['status'],'checks':{key:value['status'] for key,value in baseline['checks'].items()},'obstacle_tier_counts':baseline['metrics']['obstacle_tier_counts']}, 'cases':[]}

def facility(doc):
    return next(e for e in doc.modelspace() if (_metadata(e) or {}).get('role') == 'facility')

for name, expected in [('facility_inside_factory', 'obstacle_conflict'), ('facility_area_reduced_truthful_dimensions','facility_area_unmet'), ('road_rotated_10_degrees','road_axis_unmet')]:
    doc = ezdxf.readfile(target)
    if name == 'facility_inside_factory':
        entity = facility(doc)
        old = occupied_polygon(doc, entity.dxf.handle).centroid
        obstruction = next(o for o in scene['obstacles'] if o.get('human_confirmed') and o.get('constraint_tier') != 'uncertain_keepout')
        new = shape(obstruction['geometry']).centroid
        entity.translate(new.x-old.x, new.y-old.y, 0)
        change = {'handle':entity.dxf.handle, 'obstacle_id':obstruction['id'], 'operation':'translate actual facility footprint to the reviewed factory centre'}
    elif name == 'facility_area_reduced_truthful_dimensions':
        entity=facility(doc)
        center=occupied_polygon(doc, entity.dxf.handle).centroid
        points=list(entity.get_points('xy'))
        entity.set_points([(center.x+.5*(x-center.x),center.y+.5*(y-center.y)) for x,y in points], format='xy')
        metadata=_metadata(entity)
        metadata['width_m'] *= .5
        metadata['depth_m'] *= .5
        serialized=json.dumps(metadata,ensure_ascii=True,separators=(',',':'))
        entity.set_xdata('CAD_AGENT_SITE',[(1000,serialized[i:i+240]) for i in range(0,len(serialized),240)])
        change = {'handle':entity.dxf.handle,'facility_id':metadata['facility_id'],'operation':'halve both actual dimensions and reported dimensions; keep full program target unchanged'}
    else:
        entity=next(e for e in doc.modelspace() if (_metadata(e) or {}).get('role')=='road')
        points=list(entity.get_points('xy'))
        cx,cy=[sum(p[i] for p in points)/len(points) for i in range(2)]
        angle=math.radians(10)
        entity.set_points([(cx+(x-cx)*math.cos(angle)-(y-cy)*math.sin(angle),cy+(x-cx)*math.sin(angle)+(y-cy)*math.cos(angle)) for x,y in points],format='xy')
        change={'handle':entity.dxf.handle,'operation':'rotate actual road segment 10 degrees about its midpoint; width unchanged'}
    path=output/f'{name}.dxf'
    doc.saveas(path)
    checked=review_layout(source,path,scene,program)
    # Full temporary CAD variants are not kept in the repository.
    codes=sorted({item['code'] for item in checked['issues']})
    summary['cases'].append({'name':name,'variant_sha256':digest(path),'status':checked['status'],'expected_code':expected,'detected':checked['status']=='failed' and expected in codes,'issues':codes,'change':change,'example':next((i for i in checked['issues'] if i['code']==expected),None)})
summary['source_unchanged']=digest(source)==source_hash
summary['delivery_unchanged']=digest(target)==target_hash
args.output.write_text(json.dumps(summary,ensure_ascii=False,indent=2) + '\n')
shutil.rmtree(output)
print(json.dumps(summary,ensure_ascii=False,indent=2))
if not all(case['detected'] for case in summary['cases']) or not summary['source_unchanged'] or not summary['delivery_unchanged']:
    raise SystemExit(1)
