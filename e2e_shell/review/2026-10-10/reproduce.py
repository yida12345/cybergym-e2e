import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, '/data/cybergym-e2e/e2e_shell')
from shelllib import manifest, util
from shelllib.cc_runner import CcEnvironment, CcRunner, check_effort_evidence
from shelllib.docker_control import ContainerSpec, DockerControl
from shelllib.online_export import OnlineExporter
from shelllib.queue import TaskPool, connect
from shelllib.results import AttemptResult, StageRecord, write_result_and_receipt, verify_receipt
from shelllib.runner_adapter import AttemptOutcome
from shelllib.supervisor import WorkerPlan, _run_task_repeats, _attempt_dir_name
from shelllib.trajectory_adapter import aggregate_capture_logs
from tests.helpers import make_config, make_fake_dataset
from tests.test_online_export import _seed_finished_attempt
import prepare_cc_runtime

findings = {}

def fixture(root):
    make_fake_dataset(root)
    cfg = make_config(root)
    cfg.derive()
    data = manifest.prepare_new_experiment(cfg)
    pool = TaskPool(cfg.queue_db_path())
    pool.initialize([t['task_path'] for t in data['tasks']], 'e2e', 2, 1)
    return cfg, data, pool

def result_for(attempt, cfg, status='failed'):
    return AttemptResult(task_path='curl/arvo_66012', mode='e2e', repeat_index=1,
        generation=attempt['generation'], attempt_id=attempt['attempt_id'], scope=cfg.scope,
        launch_id='launch-a', model='glm53', claude_code_version='2.1.91',
        stage_records={s: StageRecord(stage=s, raw_status=status if s==1 else 'passed',
            normalized_status=status if s==1 else 'passed') for s in range(1,5)},
        success_stages=[1,2,3], enabled_stages=[1,2,3,4])

with tempfile.TemporaryDirectory(prefix='e2e-review-') as tmp:
    root = Path(tmp)
    cfg, data, pool = fixture(root / 'stop')
    task = pool.claim_task(0)
    stop = {'value': False}
    plan = WorkerPlan(cfg, data, 'launch-a', {}, '', {}, 0)
    def interrupted(context):
        stop['value'] = True
        return AttemptOutcome('indeterminate', 'stop_requested', context.attempt_dir)
    with patch('shelllib.supervisor.run_attempt', interrupted):
        _run_task_repeats(plan, pool, Mock(), Mock(), task, lambda: stop['value'])
    findings['interrupted_repeat_counted_done'] = pool.counts()['slots']

    cfg, data, pool = fixture(root / 'generation')
    task = pool.claim_task(0)
    directory = cfg.attempt_root / _attempt_dir_name(task, 1)
    first = pool.begin_attempt(task, 1, 0, 'launch-a', directory)
    write_result_and_receipt(directory, result_for(first, cfg), [1,2,3,4])
    pool.recover_dead_attempt(first, 'crash', receipt_valid=False)
    pool.recover_interrupted_pool()
    task2 = pool.claim_task(0)
    second = pool.begin_attempt(task2, 1, 0, 'launch-b', cfg.attempt_root / _attempt_dir_name(task2, 1))
    receipt = verify_receipt(Path(second['output_dir']), [1,2,3,4])
    findings['generation_reuses_old_directory_and_receipt'] = {
        'same_output_dir': first['output_dir'] == second['output_dir'],
        'first_generation': first['generation'], 'second_generation': second['generation'],
        'old_receipt_accepted': receipt is not None,
        'receipt_matches_current_attempt': receipt['attempt_id'] == second['attempt_id']}

    cfg, data, pool = fixture(root / 'outcome')
    task = pool.claim_task(0)
    attempt = pool.begin_attempt(task, 1, 0, 'launch-a', cfg.attempt_root / 'a')
    write_result_and_receipt(Path(attempt['output_dir']), result_for(attempt, cfg), [1,2,3,4])
    pool.recover_finished_from_receipts(lambda a: verify_receipt(Path(a['output_dir']), [1,2,3,4]) is not None, [1,2,3,4])
    findings['failed_receipt_recovered_as_success'] = {
        'result': util.read_json(Path(attempt['output_dir'])/'result.json')['result'],
        'queue_outcome': pool.finished_attempts()[0]['outcome']}

    run = root / 'capture'
    cap = run/'capture_logs'/'launch-20261010-120000-abcdef'/'raw'/'completed'/'cap1'
    cap.mkdir(parents=True)
    (cap/'request.json').write_text('{}')
    (cap/'response.json').write_text('{}')
    aggregated = aggregate_capture_logs(run, root/'aggregate')
    findings['actual_capture_launch_name_ignored'] = {
        'existing_captures': 1, 'scanned_launches': aggregated['launches'], 'aggregated_captures': aggregated['captures']}

    cfg, data, pool = fixture(root/'online')
    output = _seed_finished_attempt(cfg, outcome='success', attempt_id='review-online')
    exporter = OnlineExporter(cfg)
    exporter.run_once()
    record = json.loads((exporter.root/'successful.jsonl').read_text().splitlines()[0])
    changed = util.read_json(output/'result.json')
    changed['result'] = 'failed'
    changed['checks'][0]['score'] = 0.0
    changed['configured_success']['configured_success'] = 'failed'
    util.atomic_write_json(output/'result.json', changed)
    with contextlib.closing(connect(cfg.queue_db_path())) as db:
        db.execute("UPDATE attempts SET outcome='failed' WHERE attempt_id='review-online'")
        db.commit()
    updated = exporter.run_once()
    findings['online_has_metadata_only_and_does_not_update_frozen_result'] = {
        'published_record_keys': sorted(record), 'sharegpt_exists': (exporter.root/'sharegpt').exists(),
        'updates_reported': updated['updates'], 'successful_after_failure_update': updated['total_successful'],
        'failed_after_failure_update': updated['total_failed']}

    cfg, data, pool = fixture(root/'freeze')
    cfg.stage_validation_timeout_seconds += 123
    (cfg.frozen_dir/'projects'/'curl'/'arvo_66012'/'compile.sh').write_text('changed frozen script\n')
    (cfg.capture_proxy_dir/'proxy_retry.py').write_text('changed capture code\n')
    findings['changed_validation_budget_scripts_and_capture_accepted_on_resume'] = manifest.verify_manifest_for_resume(cfg)['verified']

    cap = root/'effort'/'completed'/'c1'
    cap.mkdir(parents=True)
    (cap/'request.json').write_text(json.dumps({'body_json': {'model':'glm53','thinking':{'type':'enabled','budget_tokens':32768}}}))
    findings['real_capture_body_json_effort_unrecognized'] = check_effort_evidence(root/'effort', 'glm53')

    runtime = root/'runtime'
    (runtime/'node'/'bin').mkdir(parents=True)
    (runtime/'node'/'bin'/'node').write_text('fake')
    (runtime/'claude-code').mkdir()
    (runtime/'claude-code'/'package.json').write_text(json.dumps({'version':'2.1.91'}))
    (runtime/'manifest.json').write_text(json.dumps({'claude_code':{'claude_code_version':'2.1.91'}}))
    try:
        prepare_cc_runtime.verify(runtime)
        findings['runtime_generated_manifest_verification'] = 'unexpected success'
    except Exception as exc:
        findings['runtime_generated_manifest_verification'] = f'{type(exc).__name__}: {exc}'

    env = CcEnvironment('http://example.invalid','dummy','glm53','high',1000,0,16,300,0,None)
    class SilentDocker:
        def exec_run(self,*args,**kwargs): return (0,'','')
        def exec_stream_lines(self,*args,**kwargs):
            return subprocess.Popen([sys.executable,'-c','import time; time.sleep(0.6)'], stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
    began = time.monotonic()
    response = CcRunner(SilentDocker()).run('fake','prompt',root/'silent.log',env=env,timeout_seconds=0.05,stop_check=lambda:'stop_requested')
    findings['silent_cc_ignores_timeout_and_stop'] = {'requested_timeout_seconds':0.05,'elapsed_seconds':round(time.monotonic()-began,2),'stop_reason':response.stop_reason}

    class FinishedDocker(SilentDocker):
        def exec_stream_lines(self,*args,**kwargs):
            return Mock(stdout=io.BytesIO(b'{"type":"system"}\n{"type":"assistant"}\n{"type":"result"}\n'),poll=lambda:0,wait=lambda:0)
    CcRunner(FinishedDocker()).run('fake','prompt',root/'tail.log',env=env,timeout_seconds=10)
    findings['exited_cc_stream_not_fully_drained'] = {'available_lines':3,'saved_lines':len((root/'tail.log').read_text().splitlines())}

    spec = ContainerSpec(image='repo/name@sha256:expected',name='x',role='agent',scope='s',memory='12g',cpus=2,pids_limit=2048,network='n',volumes={'/host':{'bind':'/tools','mode':'ro'}})
    info = {'Config':{'Image':'repo/name@sha256:wrong','Labels':spec.ownership_labels(),'Cmd':['wrong']},'HostConfig':{'Memory':1,'PidsLimit':1,'NanoCpus':1,'Binds':['/host:/wrong:rw']},'NetworkSettings':{'Networks':{'n':{}}}}
    findings['wrong_digest_limits_and_mount_accepted'] = DockerControl()._matches_record(info,spec.to_record())[0]

print(json.dumps(findings, ensure_ascii=False, indent=2))
