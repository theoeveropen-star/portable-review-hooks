"""Owner-authored literal reminders; no model calls and no review bookkeeping.

The caller authenticates the owner and supplies the canonical scope/session/turn.
Matching, delivery and final-text observation are deliberately separate operations.
All tables are additive; an existing caller transaction is never committed here.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import tempfile
import uuid
from zoneinfo import ZoneInfo

def _install_zone():
    # The install's local zone (site.env TIMEZONE -> COMPANION_TIMEZONE, else an IANA TZ),
    # used for the weekly Sunday 04:30 export and {time} notes. Falls back to UTC+8.
    for name in (os.environ.get('COMPANION_TIMEZONE'), os.environ.get('TZ')):
        name = (name or '').strip().lstrip(':')
        if name and re.fullmatch(r'[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+){0,2}', name):
            try:
                return ZoneInfo(name)
            except Exception:
                continue
    return ZoneInfo('Asia/Shanghai')


TZ = _install_zone()
SEVEN_DAYS = 7 * 86400
MAX_RULES = 128
_UNSET = object()
_CONTROL = re.compile(r'[\x00-\x1f\x7f\x85\u2028\u2029]')
_CLAUSES = re.compile(r'[。！？!?；;\n]')
_CLOCK = re.compile(r'(?<!\d)(?:[01]?\d|2[0-3])(?:[:：][0-5]\d|点(?:[0-5]?\d分?)?)(?!\d)')
_DATE = re.compile(r'(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*日|(?<!\d)\d{2}-\d{2}(?!\d)')

# Optional seed configurations (kind, triggers, note, source), not additional immutable
# rules. Replacing their triggers changes them into literal rules; editing a note keeps guards.
SEEDS = ()  # the owner writes every note; nothing is pre-filled

DDL = (
    '''CREATE TABLE IF NOT EXISTS claude_fridge_rules(
      scope TEXT NOT NULL,id TEXT NOT NULL,version INTEGER NOT NULL,priority INTEGER NOT NULL,
      kind TEXT NOT NULL,triggers_json TEXT NOT NULL,note TEXT NOT NULL,source TEXT NOT NULL,
      enabled INTEGER NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,
      PRIMARY KEY(scope,id))''',
    '''CREATE TABLE IF NOT EXISTS claude_fridge_rule_versions(
      scope TEXT NOT NULL,rule_id TEXT NOT NULL,version INTEGER NOT NULL,snapshot_json TEXT NOT NULL,
      created_at REAL NOT NULL,PRIMARY KEY(scope,rule_id,version))''',
    '''CREATE TABLE IF NOT EXISTS claude_fridge_turns(
      scope TEXT NOT NULL,session_id TEXT NOT NULL,request_id TEXT NOT NULL,draft_hash TEXT NOT NULL,
      matched_at REAL NOT NULL,outcome TEXT NOT NULL,final_hash TEXT,finished_at REAL,
      PRIMARY KEY(scope,session_id,request_id))''',
    '''CREATE TABLE IF NOT EXISTS claude_fridge_hits(
      id TEXT PRIMARY KEY,scope TEXT NOT NULL,session_id TEXT NOT NULL,request_id TEXT NOT NULL,
      rule_id TEXT NOT NULL,rule_version INTEGER NOT NULL,rule_snapshot_json TEXT NOT NULL,
      note TEXT NOT NULL,source TEXT NOT NULL,draft_hash TEXT NOT NULL,matched_at REAL NOT NULL,
      muted INTEGER NOT NULL,muted_reason TEXT,muted_until REAL,selected INTEGER NOT NULL,
      delivered_at REAL,UNIQUE(scope,session_id,request_id,rule_id))''',
    '''CREATE TABLE IF NOT EXISTS claude_fridge_feedback(
      hit_id TEXT PRIMARY KEY,scope TEXT NOT NULL,rule_id TEXT NOT NULL,value TEXT NOT NULL,
      first_voted_at REAL NOT NULL,updated_at REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS claude_fridge_mutes(
      scope TEXT NOT NULL,rule_id TEXT NOT NULL,muted_until REAL NOT NULL,
      caused_by_json TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(scope,rule_id))''',
    '''CREATE TABLE IF NOT EXISTS claude_fridge_exports(
      scope TEXT NOT NULL,period TEXT NOT NULL,body TEXT NOT NULL,created_at REAL NOT NULL,
      PRIMARY KEY(scope,period))''',
    'CREATE INDEX IF NOT EXISTS claude_fridge_hits_turn ON claude_fridge_hits(scope,session_id,request_id)',
    'CREATE INDEX IF NOT EXISTS claude_fridge_feedback_rule ON claude_fridge_feedback(scope,rule_id,first_voted_at)',
)


class FridgeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text(value, field, maximum):
    if not isinstance(value, str) or _CONTROL.search(value) or not 1 <= len(value.strip()) <= maximum:
        raise FridgeError('fridge_invalid_' + field)
    return value.strip()


def _triggers(value):
    if isinstance(value, str):
        value = re.split(r'[|\r\n]+', value)
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        raise FridgeError('fridge_invalid_triggers')
    return list(dict.fromkeys(_text(v, 'trigger', 80) for v in value))


def _now(value):
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise FridgeError('fridge_invalid_time')
    return float(value)


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class FridgeNotes:
    def __init__(self, db, lock=None, *, enabled=True):
        self.db, self.lock, self.enabled = db, lock or threading.RLock(), enabled is True
        if self.enabled:
            with self._write():
                for statement in DDL:
                    self.db.execute(statement)

    @contextmanager
    def _write(self):
        if not self.enabled:
            raise FridgeError('fridge_disabled')
        with self.lock:
            name = 'fridge_' + uuid.uuid4().hex
            self.db.execute('SAVEPOINT ' + name)
            try:
                yield
                self.db.execute('RELEASE SAVEPOINT ' + name)
            except BaseException:
                self.db.execute('ROLLBACK TO SAVEPOINT ' + name)
                self.db.execute('RELEASE SAVEPOINT ' + name)
                raise

    def _rows(self, sql, params=()):
        cursor = self.db.execute(sql, params)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _seed(self, scope, now):
        # Seeding is idempotent and cannot re-enable an edited or disabled rule.
        for number, (kind, triggers, note, source) in enumerate(SEEDS, 1):
            rid = 'rule-' + str(number)
            added = self.db.execute('INSERT OR IGNORE INTO claude_fridge_rules VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (scope,rid,1,number,kind,_json(triggers),note,source,1,now,now)).rowcount
            if added:
                self._version(scope, rid, now)

    def _version(self, scope, rid, now):
        rule = self._rule(scope, rid)
        self.db.execute('INSERT INTO claude_fridge_rule_versions VALUES(?,?,?,?,?)',
            (scope,rid,rule['version'],_json(rule),now))

    def _rule(self, scope, rid):
        rows = self._rows('SELECT * FROM claude_fridge_rules WHERE scope=? AND id=?', (scope,rid))
        if not rows:
            raise FridgeError('fridge_rule_not_found')
        row = rows[0]
        row['triggers'] = json.loads(row.pop('triggers_json'))
        row['enabled'] = bool(row['enabled'])
        return row

    def create_rule(self, scope, triggers, note, source, *, now=None):
        scope, now = _text(scope, 'scope', 200), _now(now)
        triggers, note, source = _triggers(triggers), _text(note, 'note', 180), _text(source, 'source', 300)
        with self._write():
            self._seed(scope, now)
            count, priority = self.db.execute('SELECT COUNT(*),MAX(priority) FROM claude_fridge_rules WHERE scope=?',(scope,)).fetchone()
            if count >= MAX_RULES:
                raise FridgeError('fridge_rule_limit')
            rid = 'rule-' + uuid.uuid4().hex
            self.db.execute('INSERT INTO claude_fridge_rules VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (scope,rid,1,(priority if priority is not None else -1)+1,'literal',_json(triggers),note,source,1,now,now))
            self._version(scope,rid,now)
            return self._rule_view(self._rule(scope,rid),now)

    def update_rule(self, scope, rule_id, *, note=_UNSET, source=_UNSET, triggers=_UNSET,
                    enabled=_UNSET, expected_version=_UNSET, now=None):
        scope, rule_id, now = _text(scope,'scope',200), _text(rule_id,'rule_id',80), _now(now)
        if expected_version is not _UNSET and (type(expected_version) is not int or expected_version < 1):
            raise FridgeError('fridge_invalid_version')
        with self._write():
            self._seed(scope,now)
            rule = self._rule(scope,rule_id)
            if expected_version is not _UNSET and expected_version != rule['version']:
                raise FridgeError('fridge_rule_version_conflict')
            if all(v is _UNSET for v in (note,source,triggers,enabled)):
                raise FridgeError('fridge_empty_update')
            if note is not _UNSET: rule['note'] = _text(note,'note',180)
            if source is not _UNSET: rule['source'] = _text(source,'source',300)
            if triggers is not _UNSET:
                rule['triggers'], rule['kind'] = _triggers(triggers), 'literal'
            if enabled is not _UNSET:
                if type(enabled) is not bool: raise FridgeError('fridge_invalid_enabled')
                rule['enabled'] = enabled
            self.db.execute('UPDATE claude_fridge_rules SET version=version+1,kind=?,triggers_json=?,note=?,source=?,enabled=?,updated_at=? WHERE scope=? AND id=?',
                (rule['kind'],_json(rule['triggers']),rule['note'],rule['source'],int(rule['enabled']),now,scope,rule_id))
            self._version(scope,rule_id,now)
            return self._rule_view(self._rule(scope,rule_id),now)

    @staticmethod
    def _match_rule(rule, draft, previous_user, searched_memory, now):
        kind, words = rule['kind'], rule['triggers']
        if kind == 'date':
            matched = bool(_DATE.search(draft))
        elif kind == 'send_away':
            matched = sum(draft.count(word) for word in words) >= 3
        elif kind == 'time_ref':
            referents = ('你说','你讲','你发','你提','你刚','那只','那件','那次','那天','那个','那位','这只')
            clauses = [part for part in _CLAUSES.split(draft) if any(w in part for w in words) and any(w in part for w in referents)]
            if not clauses:
                return False, None
            return True, 'same_clause_clock' if all(_CLOCK.search(part) for part in clauses) else None
        else:
            matched = any(word in draft for word in words)
        if not matched:
            return False, None
        if kind == 'sleep':
            if any(word in previous_user for word in ('困','睡','哄','晚安')):
                return True, 'user_sleep_context'
            local = datetime.fromtimestamp(now,TZ)
            if 90 <= local.hour * 60 + local.minute < 360:
                return True, 'night_0130_0600'
        if kind == 'memory' and searched_memory:
            return True, 'memory_search_used'
        if kind == 'safety' and any(word in previous_user for word in ('疼','难受','发烧','吐')):
            return True, 'user_health_context'
        if kind == 'date' and any(word in previous_user for word in ('几号','哪天','日期')):
            return True, 'user_date_question'
        return True, None

    def match(self, scope, session_id, request_id, draft, *, previous_user='', searched_memory=False, now=None):
        if not self.enabled:
            return {'hits':[], 'notes':[], 'context':''}
        scope, session_id, request_id = _text(scope,'scope',200), _text(session_id,'session_id',256), _text(request_id,'request_id',256)
        now = _now(now)
        if not isinstance(draft,str) or len(draft)>1000000 or not isinstance(previous_user,str) or len(previous_user)>100000:
            raise FridgeError('fridge_invalid_draft')
        if type(searched_memory) is not bool: raise FridgeError('fridge_invalid_search_flag')
        with self._write():
            self._seed(scope,now)
            existing = self.db.execute('SELECT 1 FROM claude_fridge_turns WHERE scope=? AND session_id=? AND request_id=?',(scope,session_id,request_id)).fetchone()
            if not existing:
                digest = _digest(draft)
                self.db.execute('INSERT INTO claude_fridge_turns VALUES(?,?,?,?,?,?,NULL,NULL)',(scope,session_id,request_id,digest,now,'pending'))
                rules = self._rows('SELECT id FROM claude_fridge_rules WHERE scope=? AND enabled=1 ORDER BY priority,id',(scope,))
                selected = 0
                for item in rules:
                    rule = self._rule(scope,item['id'])
                    matched, reason = self._match_rule(rule,draft,previous_user,searched_memory,now)
                    if not matched: continue
                    mute = self.db.execute('SELECT muted_until FROM claude_fridge_mutes WHERE scope=? AND rule_id=?',(scope,rule['id'])).fetchone()
                    muted_until = mute[0] if mute and mute[0]>now else None
                    if muted_until: reason = 'negative_feedback'
                    chosen = reason is None and selected < 2
                    if chosen: selected += 1
                    if reason is None and not chosen: reason = 'turn_limit'
                    note = rule['note'].replace('{time}',datetime.fromtimestamp(now,TZ).strftime('%H:%M')) if rule['kind']=='time_ref' else rule['note']
                    hid = 'hit-' + _digest(_json([scope,session_id,request_id,rule['id']]))[:40]
                    self.db.execute('INSERT INTO claude_fridge_hits VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                        (hid,scope,session_id,request_id,rule['id'],rule['version'],_json(rule),note,rule['source'],digest,now,int(reason is not None),reason,muted_until,int(chosen)))
            hits = self._hits(scope,session_id,request_id,MAX_RULES)
            notes = [hit for hit in hits if hit['selected'] and not hit['delivered'] and hit['outcome']=='pending']
            return {'hits':hits,'notes':notes,'context':'\n'.join(hit['note'] for hit in notes)}

    def mark_delivered(self, scope, session_id, request_id, hit_ids, *, now=None):
        scope, session_id, request_id = _text(scope,'scope',200), _text(session_id,'session_id',256), _text(request_id,'request_id',256)
        now = _now(now)
        if not isinstance(hit_ids,list) or len(hit_ids)>2 or len(set(hit_ids))!=len(hit_ids):
            raise FridgeError('fridge_invalid_hit_ids')
        hit_ids = [_text(hid,'hit_id',80) for hid in hit_ids]
        with self._write():
            turn = self.db.execute('SELECT outcome FROM claude_fridge_turns WHERE scope=? AND session_id=? AND request_id=?',(scope,session_id,request_id)).fetchone()
            if not turn or turn[0]!='pending': raise FridgeError('fridge_turn_not_pending')
            for hid in hit_ids:
                hit = self.db.execute('SELECT selected FROM claude_fridge_hits WHERE id=? AND scope=? AND session_id=? AND request_id=?',(hid,scope,session_id,request_id)).fetchone()
                if not hit or not hit[0]: raise FridgeError('fridge_hit_not_deliverable')
            for hid in hit_ids:
                self.db.execute('UPDATE claude_fridge_hits SET delivered_at=COALESCE(delivered_at,?) WHERE id=?',(now,hid))
            return [hit for hit in self._hits(scope,session_id,request_id,MAX_RULES) if hit['id'] in hit_ids]

    def finish(self, scope, session_id, request_id, final_text=None, *, outcome='completed', now=None):
        scope, session_id, request_id = _text(scope,'scope',200), _text(session_id,'session_id',256), _text(request_id,'request_id',256)
        now = _now(now)
        if outcome not in ('completed','cancelled','failed'): raise FridgeError('fridge_invalid_outcome')
        if outcome=='completed' and (not isinstance(final_text,str) or len(final_text)>1000000):
            raise FridgeError('fridge_invalid_final_text')
        final_hash = _digest(final_text) if outcome=='completed' else None
        with self._write():
            turn = self.db.execute('SELECT outcome,final_hash FROM claude_fridge_turns WHERE scope=? AND session_id=? AND request_id=?',(scope,session_id,request_id)).fetchone()
            if not turn: return []
            if turn[0]!='pending' and (turn[0]!=outcome or turn[1]!=final_hash):
                raise FridgeError('fridge_outcome_conflict')
            self.db.execute("UPDATE claude_fridge_turns SET outcome=?,final_hash=?,finished_at=? WHERE scope=? AND session_id=? AND request_id=? AND outcome='pending'",
                (outcome,final_hash,now,scope,session_id,request_id))
            return self._hits(scope,session_id,request_id,MAX_RULES)

    def _hits(self, scope, session, request, limit):
        sql = '''SELECT h.*,t.outcome,t.final_hash,f.value AS feedback FROM claude_fridge_hits h
          JOIN claude_fridge_turns t USING(scope,session_id,request_id)
          LEFT JOIN claude_fridge_feedback f ON f.hit_id=h.id
          WHERE h.scope=? AND h.session_id=?'''
        params = [scope,session]
        if request is not None:
            sql += ' AND h.request_id=?'
            params.append(request)
        sql += ' ORDER BY h.matched_at DESC,json_extract(h.rule_snapshot_json,\'$.priority\'),h.id LIMIT ?'
        params.append(limit)
        result = self._rows(sql,params)
        for row in result:
            row.pop('rule_snapshot_json')
            final_hash = row.pop('final_hash')
            row['muted'], row['selected'] = bool(row['muted']), bool(row['selected'])
            row['delivered'] = row['delivered_at'] is not None
            row['content_changed'] = final_hash != row['draft_hash'] if final_hash is not None else None
        return result

    def _rule_view(self, rule, now):
        rule = dict(rule)
        mute = self.db.execute('SELECT muted_until FROM claude_fridge_mutes WHERE scope=? AND rule_id=?',(rule['scope'],rule['id'])).fetchone()
        rule['muted_until'] = mute[0] if mute and mute[0]>now else None
        rule['muted'] = rule['muted_until'] is not None
        count = self.db.execute('SELECT COUNT(*) FROM claude_fridge_hits WHERE scope=? AND rule_id=?',(rule['scope'],rule['id'])).fetchone()[0]
        votes = dict(self.db.execute('SELECT value,COUNT(*) FROM claude_fridge_feedback WHERE scope=? AND rule_id=? GROUP BY value',(rule['scope'],rule['id'])).fetchall())
        rule['stats'] = {'hits':count,'up':votes.get('up',0),'down':votes.get('down',0)}
        return rule

    def get_panel(self, scope, session_id, request_id=None, *, limit=30, now=None):
        if not self.enabled: return {'rules':[],'hits':[],'enabled':False}
        scope, session_id, now = _text(scope,'scope',200), _text(session_id,'session_id',256), _now(now)
        if request_id is not None: request_id = _text(request_id,'request_id',256)
        if type(limit) is not int or not 1<=limit<=MAX_RULES: raise FridgeError('fridge_invalid_limit')
        with self._write():
            self._seed(scope,now)
            rows = self._rows('SELECT id FROM claude_fridge_rules WHERE scope=? ORDER BY priority,id',(scope,))
            return {'enabled':True,'rules':[self._rule_view(self._rule(scope,row['id']),now) for row in rows],
                    'hits':self._hits(scope,session_id,request_id,limit)}

    def feedback(self, scope, session_id, hit_id, value, *, now=None):
        scope, session_id, hit_id = _text(scope,'scope',200), _text(session_id,'session_id',256), _text(hit_id,'hit_id',80)
        now = _now(now)
        if value not in (None,'up','down'): raise FridgeError('fridge_invalid_feedback')
        with self._write():
            hits = self._rows('SELECT * FROM claude_fridge_hits WHERE id=? AND scope=? AND session_id=?',(hit_id,scope,session_id))
            if not hits: raise FridgeError('fridge_hit_not_found')
            hit = hits[0]
            if not hit['selected'] or hit['delivered_at'] is None: raise FridgeError('fridge_hit_not_delivered')
            old = self.db.execute('SELECT value FROM claude_fridge_feedback WHERE hit_id=?',(hit_id,)).fetchone()
            if old and old[0]==value:
                return next(row for row in self._hits(scope,session_id,hit['request_id'],MAX_RULES) if row['id']==hit_id)
            if value is None:
                self.db.execute('DELETE FROM claude_fridge_feedback WHERE hit_id=?',(hit_id,))
            else:
                self.db.execute('''INSERT INTO claude_fridge_feedback VALUES(?,?,?,?,?,?)
                  ON CONFLICT(hit_id) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''',
                  (hit_id,scope,hit['rule_id'],value,now,now))
            self._recalculate_mute(scope,hit['rule_id'],now)
            return next(row for row in self._hits(scope,session_id,hit['request_id'],MAX_RULES) if row['id']==hit_id)

    def _recalculate_mute(self, scope, rule_id, now):
        votes = self._rows('SELECT hit_id,value,updated_at FROM claude_fridge_feedback WHERE scope=? AND rule_id=? ORDER BY updated_at DESC,rowid DESC LIMIT 3',(scope,rule_id))
        if len(votes)==3 and all(v['value']=='down' for v in votes):
            causes = _json([v['hit_id'] for v in votes])
            existing = self.db.execute('SELECT caused_by_json FROM claude_fridge_mutes WHERE scope=? AND rule_id=?',(scope,rule_id)).fetchone()
            if not existing or existing[0]!=causes:
                # Repeating a vote or editing unrelated rules cannot extend mute.
                self.db.execute('INSERT OR REPLACE INTO claude_fridge_mutes VALUES(?,?,?,?,?)',(scope,rule_id,max(v['updated_at'] for v in votes)+SEVEN_DAYS,causes,now))
        else:
            self.db.execute('DELETE FROM claude_fridge_mutes WHERE scope=? AND rule_id=?',(scope,rule_id))

    def export_weekly(self, scope, directory, *, now=None):
        """Write the latest due Sunday 04:30 count snapshot once; no network/model.

        Restart catches up the latest period. An interrupted write is repaired from
        the stored immutable body, never from a newer count for that same period.
        """
        scope, now = _text(scope,'scope',200), _now(now)
        local = datetime.fromtimestamp(now,TZ)
        end = (local-timedelta(days=(local.weekday()+1)%7)).replace(hour=4,minute=30,second=0,microsecond=0)
        if end>local: end-=timedelta(days=7)
        start, period = end-timedelta(days=7), end.strftime('%Y-%m-%d')
        with self._write():
            self._seed(scope,now)
            old = self.db.execute('SELECT body FROM claude_fridge_exports WHERE scope=? AND period=?',(scope,period)).fetchone()
            if old:
                body = old[0]
            else:
                rows = self._rows('SELECT id FROM claude_fridge_rules WHERE scope=? ORDER BY priority,id',(scope,))
                counts = []
                for row in rows:
                    rule = self._rule_view(self._rule(scope,row['id']),now)
                    hits = self.db.execute('SELECT COUNT(*) FROM claude_fridge_hits WHERE scope=? AND rule_id=? AND matched_at>=? AND matched_at<?',(scope,row['id'],start.timestamp(),end.timestamp())).fetchone()[0]
                    votes = dict(self.db.execute('SELECT value,COUNT(*) FROM claude_fridge_feedback WHERE scope=? AND rule_id=? AND first_voted_at>=? AND first_voted_at<? GROUP BY value',(scope,row['id'],start.timestamp(),end.timestamp())).fetchall())
                    counts.append({'rule_id':row['id'],'note':rule['note'],'hits':hits,'up':votes.get('up',0),'down':votes.get('down',0),'muted':rule['muted'],'muted_until':rule['muted_until']})
                body = json.dumps({'schema':'companion-fridge-weekly/v1','scope':scope,'period':period,'from':start.isoformat(),'until':end.isoformat(),'rules':counts},ensure_ascii=False,indent=2)+'\n'
                self.db.execute('INSERT INTO claude_fridge_exports VALUES(?,?,?,?)',(scope,period,body,now))
        target = Path(directory) / ('fridge-notes-'+period+'-'+_digest(scope)[:10]+'.json')
        target.parent.mkdir(parents=True,exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.fridge-export-',dir=target.parent)
        try:
            with os.fdopen(descriptor,'w',encoding='utf-8') as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary,target)
            except FileExistsError:
                if target.read_text(encoding='utf-8')!=body:
                    raise FridgeError('fridge_export_conflict')
        finally:
            os.unlink(temporary)
        return {'period':period,'path':str(target),'sha256':_digest(body)}
