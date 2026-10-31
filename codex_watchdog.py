#!/usr/bin/env python3
"""Recover only the registered private turn after completion or owner death."""
import sys,time
from pathlib import Path
from codex_ephemeral import cleanup,generation,ownership,observe_group
run=Path(sys.argv[1]);m=ownership(run)
if not m:sys.exit(1)
deadline=time.monotonic()+930
while time.monotonic()<deadline:
 if (run/'done').exists() or generation(m['owner_pid'])!=m['owner_start']:break
 try:observe_group(run)
 except Exception:pass # Cleanup itself refuses unproved ownership.
 time.sleep(.05)
try:cleanup(run,allow_live_owner=True)
except Exception:sys.exit(1)
