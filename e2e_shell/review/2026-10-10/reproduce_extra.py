import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, '/data/cybergym-e2e/e2e_shell')
from shelllib import manifest, postprocess, supervisor, util
from shelllib.queue import TaskPool, connect
from tests.helpers import make_config, make_fake_dataset
from tests.test_online_export import _seed_finished_attempt

findings = {}
with tempfile.TemporaryDirectory(prefix='e2e-review-extra-') as temp:
    root = Path(temp)
    fixture = root / 'worker'
    make_fake_dataset(fixture)
    cfg = make_config(fixture)
    cfg.derive()
    data = manifest.prepare_new_experiment(cfg)
    pool = TaskPool(cfg.queue_db_path())
    pool.initialize(['curl/arvo_66012'], 'e2e', 2, 1)
    plan = supervisor.WorkerPlan(cfg, data, 'launch-a', {}, '', {}, 0)
    with patch('shelllib.supervisor.DockerControl'), patch('shelllib.supervisor.ImageManager'), patch('shelllib.supervisor.run_attempt', side_effect=RuntimeError('simulated pull failure')):
        try:
            supervisor.worker_main(plan)
        except RuntimeError:
            pass
    with contextlib.closing(connect(cfg.queue_db_path(), readonly=True)) as db:
        findings['worker_exception_marks_finished_but_keeps_task_running'] = {
            'worker_state': db.execute('SELECT state FROM workers WHERE rank=0').fetchone()[0],
            'task_state': db.execute('SELECT state FROM tasks WHERE id=1').fetchone()[0],
            'attempt_state': db.execute('SELECT state FROM attempts').fetchone()[0],
        }

    fixture = root / 'postprocess'
    make_fake_dataset(fixture)
    cfg = make_config(fixture)
    cfg.derive()
    manifest.prepare_new_experiment(cfg)
    _seed_finished_attempt(cfg)
    failed = subprocess.CompletedProcess(args=['mock'], returncode=7, stdout='', stderr='simulated failure')
    with patch('shelllib.postprocess.subprocess.run', return_value=failed):
        report = postprocess.finalize_and_summarize(cfg, allow_incomplete=True)
    findings['finalizer_error_does_not_fail_finalization'] = {'returned_normally': True, 'finalizer': report['finalizer']}

    fixture = root / 'npm'
    fixture.mkdir()
    package = fixture / 'fixture'
    package.mkdir()
    (package / 'package.json').write_text(json.dumps({'name':'@anthropic-ai/claude-code','version':'2.1.91'}))
    target = fixture / 'target'
    env = {**os.environ, 'npm_config_cache': str(fixture / 'cache'), 'npm_config_offline': 'true'}
    result = subprocess.run(['/usr/bin/npm', 'install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund', str(package), '--prefix', str(target)], capture_output=True, text=True, env=env, timeout=30)
    findings['npm_prefix_install_layout'] = {
        'returncode': result.returncode,
        'actual_local_install_dir_exists': (target/'node_modules/@anthropic-ai/claude-code').exists(),
        'installer_expected_lib_dir_exists': (target/'lib/node_modules/@anthropic-ai/claude-code').exists(),
    }
print(json.dumps(findings, ensure_ascii=False, indent=2))
