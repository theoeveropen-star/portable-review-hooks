#!/usr/bin/env python3
"""Register the process generation before replacing this launcher with Codex."""
import json,os,sys
from pathlib import Path
from codex_ephemeral import generation,ownership
run=Path(sys.argv[1]);pid=os.getpid();raw=Path(f'/proc/{pid}/stat').read_text();start=raw[raw.rfind(')')+2:].split()[19]
owner=ownership(run)
if not owner or generation(owner['owner_pid'])!=owner['owner_start']:sys.exit(1)
fd=os.open(run/'child.pending',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
with os.fdopen(fd,'w') as f:json.dump({'pid':pid,'start':start},f);f.flush();os.fsync(f.fileno())
os.replace(run/'child.pending',run/'child.json')
if not run.is_dir() or ownership(run)!=owner or generation(owner['owner_pid'])!=owner['owner_start']:sys.exit(1)
os.execvpe(sys.argv[2],sys.argv[2:],os.environ)
