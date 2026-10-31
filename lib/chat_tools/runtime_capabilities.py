"""Pure, host-only fixture capability projection; never discovers or probes tools.

The caller supplies an authoritative snapshot outside model/RPC arguments.
This isolated source accepts fixture provenance only; production ingress is not
implemented or attested here. Existence, grant and connection stay independent.
"""
import hashlib
import json
import re
import time

VERSION = 'companion-runtime-capability-snapshot/v1'
MAX_AGE_MS = 300000


def _wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def capability_evidence(snapshot, *, session_id, turn_id, now_ms):
    if snapshot is None:
        return None

    def unknown(reason):
        return {'role': 'tool', 'content': json.dumps({'source': 'runtime_capability_snapshot',
            'status': 'unknown', 'reason': reason, 'capabilities': []}, ensure_ascii=False, separators=(',', ':'))}

    try:
        if (not isinstance(snapshot, dict) or set(snapshot) != {'version', 'source', 'session_id',
                'turn_id', 'observed_at_ms', 'expires_at_ms', 'capabilities'}
                or snapshot['version'] != VERSION or snapshot['source'] != 'isolated_fixture_registry'
                or len(_wire(snapshot).encode()) > 8192):
            return unknown('snapshot_invalid')
        if (not isinstance(session_id, str) or not session_id or not isinstance(turn_id, str) or not turn_id
                or snapshot['session_id'] != session_id or snapshot['turn_id'] != turn_id):
            return unknown('snapshot_binding_mismatch')
        observed, expires = snapshot['observed_at_ms'], snapshot['expires_at_ms']
        if (any(type(value) is not int or not 0 <= value <= 9007199254740991 for value in (observed, expires, now_ms))
                or not observed <= now_ms <= expires or not 0 < expires - observed <= MAX_AGE_MS):
            return unknown('snapshot_not_current')
        values = snapshot['capabilities']
        if not isinstance(values, list) or len(values) > 8:
            return unknown('snapshot_invalid')
        selected, seen = [], set()
        for value in values:
            if (not isinstance(value, dict) or set(value) != {'capability_id', 'tool_exists', 'grant', 'connection', 'scopes'}
                    or not isinstance(value['capability_id'], str)
                    or not re.fullmatch(r'[a-z][a-z0-9_.:-]{0,95}', value['capability_id'])
                    or value['capability_id'] in seen
                    or not (value['tool_exists'] is None or type(value['tool_exists']) is bool)
                    or value['grant'] not in ('granted', 'revoked', 'unknown')
                    or value['connection'] not in ('connected', 'disconnected', 'unknown')
                    or not isinstance(value['scopes'], list) or len(value['scopes']) > 8
                    or any(not isinstance(scope, str) or not re.fullmatch(r'[a-z][a-z0-9_.:-]{0,63}', scope) for scope in value['scopes'])
                    or len(set(value['scopes'])) != len(value['scopes'])
                    or value['grant'] == 'granted' and not value['scopes']):
                return unknown('snapshot_invalid')
            seen.add(value['capability_id'])
            selected.append({'capability_id': value['capability_id'], 'tool_exists': value['tool_exists'],
                'grant': value['grant'], 'connection': value['connection'], 'scopes': sorted(value['scopes'])})
        report = {'source': 'runtime_capability_snapshot', 'status': 'current',
            'provenance': 'isolated_fixture_registry', 'snapshot_sha256': hashlib.sha256(_wire(snapshot).encode()).hexdigest(),
            'observed_at_ms': observed, 'expires_at_ms': expires,
            'session_bound': True, 'turn_bound': True,
            'capabilities': sorted(selected, key=lambda value: value['capability_id'])}
        return {'role': 'tool', 'content': json.dumps(report, ensure_ascii=False, separators=(',', ':'))}
    except (ValueError, TypeError, KeyError, UnicodeError):
        return unknown('snapshot_invalid')


# Registered domain aliases are a lookup aid, never permission evidence.
DOMAIN_ALIASES = {
    'mail': ('gmail', 'email', '邮箱', '邮件', '信箱', '邮件账户'),
    'drive': ('google drive', 'gdrive', '云盘', '网盘', '云端硬盘', '共享盘', '共享硬盘', 'shared drive'),
    'filesystem': ('filesystem', '文件系统', '本地文件', '服务器文件'),
    'calendar': ('calendar', '日历'),
    'history': ('history', '历史', '归档'),
    'photo': ('photo', '相册', '照片'),
}
DOMAIN_NAMES = {'gmail': 'mail', 'mail': 'mail', 'google_drive': 'drive', 'gdrive': 'drive',
                'drive': 'drive', 'filesystem': 'filesystem', 'calendar': 'calendar',
                'history': 'history', 'photo': 'photo'}

# Gate evidence delivery, not truth. Quotes and foreign speakers cannot become
# assistant capability assertions. Domain hints never become permission proof.
QUOTES = r'```[\s\S]*?```|`[^`\n]*`|[“「『][^”」』\n]*[”」』]|"[^"\n]*"'
UNCERTAIN = r'不确定|不记得|记不清|不知道|没把握|无法确认|不能确认|尚不能确认|是否|能否|能不能|有没有|可不可以|not sure|do not remember|don.t remember|cannot confirm|can.t confirm|whether'
HYPOTHETICAL = r'如果|假如|假设|要是|倘若|希望|想要|愿望|假定|\bif\b|\bperhaps\b|\bmight\b|\bhope\b|\bwish\b'
CAPABILITY_NOUN = r'工具|功能|入口|接口|权限|授权|连接|能力|账户|账号|邮箱|tool|function|connector|permission|grant|connection|access|account|capabilit'
ASSERTION = r'有|没有|无|不具备|具备|存在|缺|已|未|被|在|可用|不可用|未知|不明|撤销|撤回|撤权|断开|允许|禁止|失效|生效|有效|开通|禁用|启用|connected|disconnected|granted|revoked|available|unavailable|unknown|\bhas\b|\bhave\b|\bis\b|\bare\b|\black\b'
ABILITY_ACTION = r'(?:能|可以|能够|不能|无法|无权|有权|获准|允许).{0,16}(?:查|读|写|访问|搜索|检索|打开|调用|使用|连接|获取|发送|发邮件|发信)|\b(?:can|cannot|can.t|unable|allowed|authorized)\b.{0,18}\b(?:read|write|access|search|query|open|use|connect|send)\b'
# Third-person labels are examples; arbitrary named subjects also fail the
# positive owner/start check rather than being inferred to mean the assistant.
FOREIGN = r'^(?:你|您|他|她|它|用户|对方|朋友|同事|管理员|客户|我(?:们)?(?:的)?(?:朋友|同事|家人|父亲|母亲|爸爸|妈妈|姐姐|妹妹|哥哥|弟弟|老师|客户|用户))|(?:说|表示|声称|认为|告诉我).{0,6}我|\b(?:he|she|they|you)\b|\b(?:said|says|claims|reported)\b'
SELF = r'^(?:(?:但|但是|不过|而且|并且|所以|因此|其实|也|仍然|确实|目前|当前|现在|本轮|这轮|这次|这里|这边|已经|暂时|尚且)\s*)*(?:我(?:们|这边)?|本助手|助手当前|i\b|we\b)'
IMPLICIT = r'^(?:(?:但|但是|不过|而且|并且|所以|因此|其实|也|仍然|确实|目前|当前|现在|本轮|这轮|这次|这里|这边|已经|暂时|尚且)\s*)*(?:没有|无|有|不能|无法|能够|可以|能|已|未|尚未|授权|权限|连接|工具|功能|入口|接口|当前列表|注册表)'


def _unquoted(value):
    return re.sub(QUOTES, lambda m: ' ' * len(m.group()), value)


def _domains(value):
    lower = value.lower()
    return {domain for domain, aliases in DOMAIN_ALIASES.items() if any(alias in lower for alias in aliases)}


def claim_domains(draft, current_user=''):
    if not isinstance(draft, str): return set()
    user_domains = _domains(_unquoted(current_user)) if isinstance(current_user, str) else set()
    fallback = user_domains if len(user_domains) == 1 else set()
    selected = set()
    # Strong sentence boundaries reset owner/domain carry. Commas retain only
    # adjacent local domain context; a foreign/quoted clause cannot establish it.
    for match in re.finditer(r'([^。！？!?;；\n]+)([。！？!?;；\n]|$)', _unquoted(draft)):
        sentence, end = match.groups()
        if end in ('?', '？'): continue
        previous = set()
        for clause in re.split(r'[,，]', sentence):
            clause = clause.strip()
            if not clause:
                previous = set(); continue
            direct = _domains(clause)
            foreign = re.search(FOREIGN, clause, re.I)
            starts_domain = any(clause.lower().startswith(alias) for aliases in DOMAIN_ALIASES.values() for alias in aliases)
            own = not foreign and (re.search(SELF, clause, re.I) or re.search(IMPLICIT, clause, re.I) or starts_domain)
            if re.search(HYPOTHETICAL, clause, re.I) or foreign:
                previous = set(); continue
            # Honest memory uncertainty/questions are not factual capability claims.
            # Only this clause is skipped; an adjacent explicit fact survives.
            uncertain = re.search(UNCERTAIN, clause, re.I)
            fact = not uncertain and own and ((re.search(CAPABILITY_NOUN, clause, re.I)
                    and re.search(ASSERTION, clause, re.I)) or re.search(ABILITY_ACTION, clause, re.I))
            # An address or remembered event is not a registry/permission layer.
            if re.search(r'邮箱地址|邮件地址|记起|想起|收到.{0,6}邮件|记得|想不起', clause): fact = False
            domains = direct or previous or fallback
            if fact and domains: selected.update(domains)
            # Domain-only bridge such as "关于邮箱" may support the immediately
            # adjacent self claim, but third-person/quoted text above never does.
            previous = direct if (own or re.match(r'^(?:关于|至于|说到)', clause)) else set()
    return selected


def capability_for_claim(evidence, draft, *, now_ms=None, current_user=''):
    if evidence is None: return None
    domains = claim_domains(draft, current_user)
    if not domains: return None
    value = json.loads(evidence['content'])
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if (value['status'] == 'current' and (type(now_ms) is not int
            or not value['observed_at_ms'] <= now_ms <= value['expires_at_ms'])):
        value = {'source': 'runtime_capability_snapshot', 'status': 'unknown',
                 'reason': 'snapshot_not_current', 'capabilities': []}
    if value['status'] == 'current':
        value['capabilities'] = [row for row in value['capabilities'] if
            DOMAIN_NAMES.get(row['capability_id'].split('.')[0].split(':')[0]) in domains]
        if not value['capabilities']:
            value = {'source': 'runtime_capability_snapshot', 'status': 'unknown',
                     'reason': 'registered_domain_not_supplied', 'capabilities': []}
    return {'role': 'tool', 'content': json.dumps(value, ensure_ascii=False, separators=(',', ':'))}


def snapshot_from_registry(*, registry, grants, connections, session_id, turn_id, now_ms):
    """Read-only host adapter for already captured registry/grant/connection maps.

    Inputs are bound fixture snapshots, not model text. Each has source, session_id,
    turn_id, observed_at_ms, expires_at_ms and records. Registry records are
    {capability_id: [registered_tool_name, ...]}, grants are
    {capability_id: {status, scopes}}, connections are {capability_id: status}.
    Missing/stale grant or connection data stays unknown; registry absence does
    not assert that a tool is unavailable. Production authentication is separate.
    """
    def current(value):
        return (isinstance(value, dict) and value.get('source') == 'isolated_fixture_registry'
            and value.get('session_id') == session_id and value.get('turn_id') == turn_id
            and all(type(value.get(k)) is int for k in ('observed_at_ms', 'expires_at_ms'))
            and value['observed_at_ms'] <= now_ms <= value['expires_at_ms']
            and 0 < value['expires_at_ms'] - value['observed_at_ms'] <= MAX_AGE_MS
            and isinstance(value.get('records'), dict))
    if type(now_ms) is not int or not current(registry):
        return None
    rows = []
    for identifier, names in registry['records'].items():
        if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n for n in names):
            continue
        grant = grants['records'].get(identifier, {}) if current(grants) else {}
        connection = connections['records'].get(identifier, 'unknown') if current(connections) else 'unknown'
        if not isinstance(grant, dict): grant = {}
        rows.append({'capability_id': identifier, 'tool_exists': True,
            'grant': grant.get('status', 'unknown'), 'scopes': grant.get('scopes', []),
            'connection': connection})
    result = {'version': VERSION, 'source': 'isolated_fixture_registry',
        'session_id': session_id, 'turn_id': turn_id,
        'observed_at_ms': max(v['observed_at_ms'] for v in (registry, grants, connections) if current(v)),
        'expires_at_ms': min(v['expires_at_ms'] for v in (registry, grants, connections) if current(v)),
        'capabilities': rows}
    report = capability_evidence(result, session_id=session_id, turn_id=turn_id, now_ms=now_ms)
    return result if report and json.loads(report['content'])['status'] == 'current' else None
