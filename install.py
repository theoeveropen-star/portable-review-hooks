#!/usr/bin/env python3
"""Install into a new owner directory; never replace existing source or data."""
import argparse
import json
from pathlib import Path
import shutil

parser = argparse.ArgumentParser()
parser.add_argument('--prefix', type=Path, required=True)
args = parser.parse_args()
prefix = args.prefix.expanduser().resolve()
if prefix.exists():
    raise SystemExit('prefix_exists: choose a new directory; preserve previous data')
source = Path(__file__).resolve().parent
prefix.mkdir(parents=True, mode=0o700)
for name in ('lib', 'review_host.py', 'portable_client.py', 'hook_client.py', 'example_recall.py', 'prefetch_reuse.py', 'README.md', 'INTERFACE.md', 'PREFETCH.md', 'codex_review_host.py', 'codex_review_hook.py', 'codex_read_mcp.py', 'codex_read_files.py', 'codex_launch.py', 'codex_watchdog.py', 'codex_ephemeral.py', 'config.codex.example.json', 'CODEX-README.md', '使用说明与注意事项.pdf'):
    p = source / name
    if p.is_dir(): shutil.copytree(p, prefix / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    else: shutil.copyfile(p, prefix / name)
(prefix / 'persona.local.json').write_text(json.dumps({'user_name': '用户', 'companion_name': '助手',
    'user_pronoun': 'TA', 'user_aliases': [], 'companion_aliases': [], 'pet_names': []}, ensure_ascii=False, indent=2) + '\n')
(prefix / 'config.example.json').write_text(json.dumps({'claude_command': ['claude'],
    'cli_version': '2.1.284', 'turn_timeout_seconds': 180}, indent=2) + '\n')
print(str(prefix))
