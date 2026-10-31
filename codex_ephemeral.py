"""Private per-turn cleanup: never follow the linked user credential."""
import json,os,shutil,signal,stat,time,uuid
from pathlib import Path

def generation(pid):
    try:
        s=Path('/proc',str(pid),'stat').read_text();f=s[s.rfind(')')+2:].split()
        return None if f[0]=='Z' else f[19]
    except (OSError,IndexError):return None

def mark(run):
    marker={'version':'portable-codex-ephemeral/v1','turn':run.name,'owner_pid':os.getpid(),'owner_start':generation(os.getpid())}
    fd=os.open(marker_path(run),os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(marker,f);f.flush();os.fsync(f.fileno())
    return marker

def marker_path(run):return run.parent/('.portable-codex-'+run.name+'.json')
def members_path(run):return run.parent/('.portable-members-'+run.name+'.json')

def group_members(pgid):
    members={}
    for name in os.listdir('/proc'):
        if not name.isdecimal():continue
        path=Path('/proc',name,'stat')
        try:
            data=path.read_text();f=data[data.rfind(')')+2:].split()
            if len(f)<20:raise ValueError('short_proc_stat')
            if f[0]!='Z' and int(f[2])==pgid:members[int(name)]=f[19]
        except FileNotFoundError:
            if path.parent.exists():raise RuntimeError('proc_sample_incomplete')
        except ProcessLookupError:pass
    return members

def registered_child(run):
    try:
        child=json.loads((run/'child.json').read_text());pid=child['pid'];start=child['start']
        if type(pid) is not int or pid<=1 or not isinstance(start,str):raise ValueError('invalid_child')
        return pid,start
    except FileNotFoundError:return None

def group_absent(pgid):
    try:os.killpg(pgid,0);return False
    except ProcessLookupError:return True

def observe_group(run):
    child=registered_child(run)
    if not child:return
    pid,start=child
    if generation(pid)!=start:return
    if os.getpgid(pid)!=pid:raise RuntimeError('child_not_group_leader')
    members=group_members(pid)
    if generation(pid)!=start or members.get(pid)!=start:return
    path=members_path(run)
    try:previous=json.loads(path.read_text())
    except FileNotFoundError:previous={}
    if not isinstance(previous,dict):raise ValueError('invalid_members')
    merged={**previous,**{str(k):v for k,v in members.items()}}
    if len(merged)>2048:raise ValueError('members_overflow')
    if merged==previous:return
    tmp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'w') as f:json.dump(merged,f);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:tmp.unlink(missing_ok=True)

def ownership(run):
    try:
        if str(uuid.UUID(run.name))!=run.name:return None
        try:
            info=run.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:return None
        except FileNotFoundError:pass
        mpath=marker_path(run);ms=mpath.lstat()
        if not stat.S_ISREG(ms.st_mode) or ms.st_uid!=os.getuid() or stat.S_IMODE(ms.st_mode)!=0o600 or ms.st_size>1024:return None
        m=json.loads(mpath.read_text())
        if set(m)!={'version','turn','owner_pid','owner_start'} or m['version']!='portable-codex-ephemeral/v1' or m['turn']!=run.name or type(m['owner_pid']) is not int or not isinstance(m['owner_start'],str):return None
        return m
    except (OSError,ValueError,TypeError):return None

def cleanup(run,allow_live_owner=False):
    m=ownership(run)
    if not m: return False
    if not allow_live_owner and generation(m['owner_pid'])==m['owner_start']:return False
    if not run.exists():members_path(run).unlink(missing_ok=True);marker_path(run).unlink(missing_ok=True);return True
    child=registered_child(run)
    if child:
        pid,start=child
        try:known=json.loads(members_path(run).read_text())
        except FileNotFoundError:known={}
        if not isinstance(known,dict):raise ValueError('invalid_members')
        limit=time.monotonic()+5
        while time.monotonic()<limit:
            if group_absent(pid):break
            members=group_members(pid)
            anchors=[(p,s) for p,s in members.items() if (p==pid and s==start) or known.get(str(p))==s]
            if members and not anchors:raise RuntimeError('group_generation_unproven')
            # The same live anchor before and after sampling proves the group
            # generation during this snapshot. Signal pidfds, never a numeric
            # group that could be recycled between validation and kill.
            if members and not any(generation(p)==s for p,s in anchors):continue
            for member,member_start in members.items():
                try:
                    fd=os.pidfd_open(member)
                    try:
                        if generation(member)==member_start:signal.pidfd_send_signal(fd,signal.SIGKILL)
                    finally:os.close(fd)
                except ProcessLookupError:pass
            # Kernel group absence is required, not merely a snapshot with no
            # live leader. This conservatively retains a group with zombies.
            time.sleep(.03)
        else:raise RuntimeError('group_still_present')
    shutil.rmtree(run)
    members_path(run).unlink(missing_ok=True)
    marker_path(run).unlink(missing_ok=True)
    return True

def recover(state):
    for marker in state.glob('.portable-codex-*.json'):
        turn=marker.name.removeprefix('.portable-codex-').removesuffix('.json')
        run=state/turn
        if ownership(run):cleanup(run)
