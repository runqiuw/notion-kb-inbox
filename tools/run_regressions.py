"""Run isolated regression scripts in separate interpreters; fail on any error."""
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
results = []
env = dict(os.environ)
for name in ('GCP_PROJECT', 'NOTION_TOKEN', 'TELEGRAM_BOT_TOKEN', 'NOTION_MATERIAL_DB', 'NOTION_THOUGHT_DB',
             'LLM_BASE_URL', 'LLM_MODEL', 'LLM_API_KEY', 'SERVICE_URL', 'TASKS_SA_EMAIL', 'TELEGRAM_ALLOWED_USER_ID',
             'TELEGRAM_WEBHOOK_SECRET'):
    env[name] = ''
env.update(KB_OFFLINE_TEST_MODE='1', PYTHONPATH=str(root / 'tools/offline_test_bootstrap'))
for path in sorted((root / 'tools').glob('test_*.py')):
    result = subprocess.run([sys.executable, str(path)], cwd=root, capture_output=True, text=True, timeout=90, env=env)
    results.append({'test': path.name, 'passed': result.returncode == 0})
    print(path.name, 'PASS' if result.returncode == 0 else 'FAIL', flush=True)
    if result.returncode:
        print((result.stdout + '\n' + result.stderr)[-7000:], flush=True)
print(json.dumps({'total': len(results), 'passed': sum(r['passed'] for r in results)}), flush=True)
sys.exit(0 if all(r['passed'] for r in results) else 1)
