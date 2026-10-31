"""Only synthetic files and processes; no real account or model requests."""
import json,os,signal,sqlite3,subprocess,sys,tempfile,time,uuid
from pathlib import Path
from codex_ephemeral import recover,generation,mark,cleanup
ROOT=Path(__file__).parent
def wait(pred):
    end=time.monotonic()+8
    while time.monotonic()<end:
        if pred():return
        time.sleep(.025)
    raise AssertionError('wait_timeout')
with tempfile.TemporaryDirectory() as td:
    state=Path(td)/'state';state.mkdir(mode=0o700)
    auth=Path(td)/'auth.json';auth.write_text('synthetic-credential-unchanged')
    # Simulate active Codex creating a real SQLite journal with sensitive data.
    owner_code='''import json,os,subprocess,sys,time,uuid\nfrom pathlib import Path\nfrom codex_ephemeral import mark\nstate,auth,root=map(Path,sys.argv[1:]);run=state/str(uuid.uuid4());run.mkdir(mode=0o700);mark(run);home=run/'home';home.mkdir(mode=0o700);os.symlink(auth,home/'auth.json')\nw=subprocess.Popen([sys.executable,str(root/'codex_watchdog.py'),str(run)],start_new_session=True)\nscript="import sqlite3,time,sys; c=sqlite3.connect(sys.argv[1]); c.execute('create table t (v text)'); c.execute('insert into t values (?)',('SYNTHETIC_PRIVATE_REVIEW_MARKER',)); c.commit(); time.sleep(60)"\np=subprocess.Popen([sys.executable,str(root/'codex_launch.py'),str(run),sys.executable,'-c',script,str(home/'logs_2.sqlite')],start_new_session=True)\nprint(json.dumps({'run':str(run),'watcher':w.pid,'child':p.pid}),flush=True);time.sleep(60)'''
    env={**os.environ,'PYTHONPATH':str(ROOT),'PYTHONDONTWRITEBYTECODE':'1'}
    for watcher_failure in (False,True):
        owner=subprocess.Popen([sys.executable,'-c',owner_code,str(state),str(auth),str(ROOT)],stdout=subprocess.PIPE,text=True,env=env)
        info=json.loads(owner.stdout.readline());run=Path(info['run']);watch=info['watcher'];child=info['child']
        wait(lambda:(run/'home'/'logs_2.sqlite').exists() and (run/'child.json').exists())
        wait(lambda:sqlite3.connect(run/'home'/'logs_2.sqlite').execute("select count(*) from sqlite_master where name='t'").fetchone()[0]==1)
        if watcher_failure:os.kill(watch,signal.SIGKILL)
        os.kill(owner.pid,signal.SIGKILL);owner.wait(timeout=3)
        if watcher_failure:recover(state)
        wait(lambda:not run.exists())
        wait(lambda:generation(child) is None)
        assert auth.read_text()=='synthetic-credential-unchanged'
    # Unknown UUID directories and credential originals are never removed.
    unknown=state/str(uuid.uuid4());unknown.mkdir(mode=0o700);(unknown/'original').write_text('protected')
    recover(state);assert (unknown/'original').read_text()=='protected'
    # A real descendant remains after its registered group leader exits.
    parent_code='''import json,os,subprocess,sys,time,uuid\nfrom pathlib import Path\nfrom codex_ephemeral import mark\nstate,root=map(Path,sys.argv[1:]);run=state/str(uuid.uuid4());run.mkdir(mode=0o700);mark(run);(run/'home').mkdir(mode=0o700)\nw=subprocess.Popen([sys.executable,str(root/'codex_watchdog.py'),str(run)],start_new_session=True)\nscript="import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); Path(sys.argv[1]).write_text(str(p.pid));\\nwhile not Path(sys.argv[2]).exists():time.sleep(.01)"\np=subprocess.Popen([sys.executable,str(root/'codex_launch.py'),str(run),sys.executable,'-c',script,str(run/'grandchild'),str(run/'exit')],start_new_session=True)\nprint(json.dumps({'run':str(run),'leader':p.pid}),flush=True);p.wait();(run/'reaped').touch();time.sleep(60)'''
    owner=subprocess.Popen([sys.executable,'-c',parent_code,str(state),str(ROOT)],stdout=subprocess.PIPE,text=True,env=env)
    info=json.loads(owner.stdout.readline());run=Path(info['run']);leader=info['leader']
    wait(lambda:(run/'grandchild').exists());grandchild=int((run/'grandchild').read_text());snapshot=state/('.portable-members-'+run.name+'.json')
    wait(lambda:snapshot.exists() and str(grandchild) in json.loads(snapshot.read_text()))
    (run/'exit').touch();wait(lambda:(run/'reaped').exists());assert generation(leader) is None and generation(grandchild) is not None
    os.kill(owner.pid,signal.SIGKILL);owner.wait(timeout=3)
    wait(lambda:not run.exists());wait(lambda:generation(grandchild) is None)
    # A live unrelated group with an unproved generation is neither killed nor cleaned.
    unrelated=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
    try:
        run=state/str(uuid.uuid4());run.mkdir(mode=0o700);mark(run)
        (run/'child.json').write_text(json.dumps({'pid':unrelated.pid,'start':'wrong-generation'}))
        try:cleanup(run,allow_live_owner=True);raise AssertionError('unproved_group_cleaned')
        except RuntimeError as error:assert str(error)=='group_generation_unproven'
        assert unrelated.poll() is None and run.exists()
    finally:
        os.killpg(unrelated.pid,signal.SIGKILL);unrelated.wait(timeout=3)
        assert cleanup(run,allow_live_owner=True)
print('passed: SQLite, SIGKILL, watcher loss, leader exits before real descendant, unproved group refusal, credential and unowned directory preservation')
