"""Opt-in generated-pixel benchmark. Never accepts an existing workspace."""

import argparse
import json
from pathlib import Path
import platform
import statistics
import tempfile
import time

from PIL import __version__ as pillow_version
from src.jcc_samples import SampleStore
from src.jcc_samples import workflow as wf
from src.jcc_annotation_ui.server import AnnotationServer
from src.jcc_annotation_ui.workspace import AnnotationWorkspace, EDITABLE_FIELDS
from src.jcc_annotation_ui.tests.scale_fixture import make_scale_fixture


def measure(operation, repeats=1):
    samples=[];value=None
    for _ in range(repeats):
        started=time.perf_counter();value=operation();samples.append(time.perf_counter()-started)
    return {"seconds":[round(n,6) for n in samples],"median_seconds":round(statistics.median(samples),6)},value


def anchor(view):
    return {"sample_id":view['record']['sample_id'],"sample_kind":"synthetic",
            "expected_revision":view['revision'],"view_token":view['view_token']}


def edit(view):
    return {**anchor(view),"fields":{k:view['record']['labels'][k] for k in EDITABLE_FIELDS},
            "conditions":view['conditions'],"occlusions":view['occlusions'],"annotator_id":"synthetic-perf"}


def benchmark(root, fixture):
    ids=fixture['sample_ids'];sid=ids[0];baseline={};optimized={}
    def legacy_load():
        store=SampleStore(root)
        with store._locked(): return store._load()
    print('MEASURE legacy full scans',flush=True)
    for operation in ('initialization','list','sample_detail'):
        baseline[operation],_=measure(legacy_load)
    def legacy_image():
        store=SampleStore(root)
        with store._locked():
            data=store._load();asset=data['assets'][sid]
            raw=wf._read_file(store._path(asset['path']),wf.MAX_IMAGE_BYTES,private=True)
            wf._image_info(raw)
    baseline['image'],_=measure(legacy_image)
    print('MEASURE optimized inventory / selected image / preflight',flush=True)
    optimized['initialization'],ui=measure(lambda:AnnotationWorkspace(root,sample_kind='synthetic'))
    optimized['list_before_any_decode'],listing=measure(ui.samples,3)
    optimized['list_before_any_decode']['verification']=listing['verification']
    optimized['first_sample_detail'],view=measure(lambda:ui.sample(sid))
    optimized['first_sample_detail']['verification']=view['verification']
    optimized['image_after_detail'],_=measure(lambda:ui.image(anchor(view)))
    optimized['full_preflight_first'],preflight=measure(ui.verify)
    optimized['full_preflight_first']['verification']=preflight['verification']
    optimized['list_after_preflight'],_=measure(ui.samples,3)
    optimized['sample_detail_after_preflight'],_=measure(lambda:ui.sample(ids[1]),3)
    optimized['image_after_preflight'],_=measure(lambda:ui.image(anchor(view)),3)
    optimized['full_preflight_warm'],_=measure(ui.verify,3)

    print('MEASURE first submit from separate cold instance and warm writes',flush=True)
    cold_writer=AnnotationWorkspace(root,sample_kind='synthetic')
    cold_view=cold_writer.sample(sid)
    optimized['save_without_preflight_cold_instance'],_=measure(lambda:cold_writer.annotate(edit(cold_view)))
    cold_writer.close()
    view=ui.sample(sid)
    optimized['save_after_preflight'],_=measure(lambda:ui.annotate(edit(view)))
    view=ui.sample(sid)
    review={**anchor(view),"reviewer_id":"synthetic-perf-review","evidence_ref":"synthetic-perf-check",
            "status":"independently_reviewed","confirmation":"synthetic_software_check"}
    optimized['review_after_preflight'],_=measure(lambda:ui.review(review))
    summary=ui.samples()
    result={"date_local":"2026-11-02","software_only":True,"real_candidate_images_or_manifests_read":False,
            "fixture":{k:v for k,v in fixture.items() if k!='sample_ids'},
            "python":platform.python_version(),"pillow":pillow_version,
            "timing_note":"Same generated workspace and process. OS file cache was not purged; no decoded proof persisted across instances. Baseline is original full SampleStore._load path; initialization/list/detail each performed that scan in the previous UI.",
            "legacy_full_decode_path":baseline,"optimized":optimized,
            "final_synthetic_samples":len(summary['samples']),"real_samples":0,
            "write_note":"All writes are explicit software flow checks on generated fixtures, with all labels unknown. The cold-instance save includes full decoding of 99 not-yet-viewed assets.",
            "cleanup":"Temporary generated workspace and in-memory proofs are removed at process exit."}
    return result,ui


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--serve-for-browser',action='store_true')
    args=parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix='jcc-ui-scale-synthetic-') as temporary:
        parent=Path(temporary).resolve();root=parent/'store'
        fixture=make_scale_fixture(root,progress=lambda n:print(f'GENERATED {n}/100',flush=True))
        result,ui=benchmark(root,fixture)
        Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'images':fixture['images'],'encoded_mebibytes':round(fixture['total_encoded_bytes']/1024**2,2),
                          'legacy_seconds':{k:v['median_seconds'] for k,v in result['legacy_full_decode_path'].items()},
                          'optimized_seconds':{k:v['median_seconds'] for k,v in result['optimized'].items()}},ensure_ascii=False),flush=True)
        ui.close()
        if args.serve_for_browser:
            server=AnnotationServer(AnnotationWorkspace(root,sample_kind='synthetic'))
            wf._write_new(parent/'session-url',server.session_url.encode())
            print('BROWSER_SESSION_DIR '+str(parent),flush=True)
            try: server.run()
            except KeyboardInterrupt: server.stopping.set()
            finally: server.server_close()
        print('SYNTHETIC_SCALE_RUN_FINISHED',flush=True)


if __name__=='__main__':
    main()
