"""Lossless visible-dialogue projection for Claude Code 2.1.280 JSONL.

No upstream trimmer is called: aichat 1.29.0 also rewrites tool inputs and
estimates savings with characters/4. We retain inputs, signatures and images.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import re

ENGINE = 'companion-cc-lossless-v1'
MAX_SNAPSHOT = 128 * 1024 * 1024


class SlimError(ValueError):
    pass


def digest(value):
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def blocks(record):
    content = record.get('message', {}).get('content', [])
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}]
    if not isinstance(content, list) or any(not isinstance(b, dict) for b in content):
        raise SlimError('invalid_message_blocks')
    return content


def read_records(raw):
    if len(raw) > MAX_SNAPSHOT:
        raise SlimError('snapshot_too_large')
    try:
        records = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
    except (UnicodeError, ValueError):
        raise SlimError('invalid_transcript') from None
    if any(not isinstance(r, dict) for r in records):
        raise SlimError('invalid_transcript')
    return records


def active_chain(records, final_text=None, allow_tail=False):
    """Follow a committed leaf's parent links, never the entire disk history.

    Compact boundaries terminate the chain. Native verification independently
    checks what the installed loader actually sends before a binding can move.
    With allow_tail (read-only preview), an in-progress turn after the committed
    final is left out and counted instead of refusing the whole preview.
    """
    tail = 0
    nodes = {r['uuid']: r for r in records if isinstance(r.get('uuid'), str)}
    messages = [r for r in records if r.get('type') in ('user', 'assistant') and not r.get('isSidechain')]
    if not messages:
        return [], {'native_compacted': False, 'archived_records': len(records)}
    leaf = messages[-1]
    if final_text is not None:
        eligible = [r for r in messages if r['type'] == 'assistant' and
                    ''.join(b.get('text', '') for b in blocks(r) if b.get('type') == 'text') == final_text]
        if not eligible:
            raise SlimError('last_assistant_not_in_transcript')
        leaf = eligible[-1]
        # A compact summary after the committed final is the active state.
        later = messages[messages.index(leaf) + 1:]
        if any(r.get('isCompactSummary') for r in later):
            leaf = later[-1]
        elif later:
            if not allow_tail:
                raise SlimError('uncommitted_transcript_tail')
            tail = len(later)
    chain, seen, compacted = [], set(), False
    current = leaf
    while current:
        identifier = current.get('uuid')
        if not identifier or identifier in seen:
            raise SlimError('invalid_message_chain')
        seen.add(identifier)
        if current.get('type') == 'system' and current.get('subtype') == 'compact_boundary':
            compacted = True
            break
        if current.get('type') in ('user', 'assistant'):
            if current.get('isSidechain'):
                raise SlimError('sidechain_in_active_chain')
            chain.append(current)
            if current.get('isCompactSummary'):
                compacted = True
                # Some versions retain a parent pointer to archived originals.
                break
        parent = current.get('parentUuid')
        if parent is not None and parent not in nodes:
            raise SlimError('missing_message_parent')
        current = nodes.get(parent)
    chain.reverse()
    chain = attach_parallel_results(chain, records)
    return chain, {'native_compacted': compacted, 'archived_records': len(records) - len(chain),
                   'excluded_uncommitted_records': tail,
                   'full_precompact_originals_restored': False if compacted else None}


def attach_parallel_results(chain, records):
    """Pull back the off-chain records of a parallel tool round.

    Sibling calls are restored first (see parallel_siblings). If that shape
    cannot be proved complete, fall back to the previous results-only recovery
    so this never refuses a transcript the earlier code accepted.
    """
    try:
        widened = parallel_siblings(chain, records)
        if len(widened) != len(chain):
            return _attach_results(widened, records)
    except SlimError:
        pass
    return _attach_results(chain, records)


def parallel_siblings(chain, records):
    """Restore later tool_use records of one provider message.

    CC writes each parallel tool_use of one assistant message as its own
    record, each linked to the previous call, and hangs every result under its
    own call. When an earlier call's result is written last, the leaf path
    runs through that result and skips the later calls together with their
    results (CC's own loader puts them back). Only a single tool_use-only
    assistant child with the same message.id and session as its parent, written
    before the leaf and answered by exactly one result-only record, is restored
    right after that parent. Results are then attached by _attach_results.
    """
    if not chain:
        return chain
    positions = {id(r): i for i, r in enumerate(records)}
    limit = positions.get(id(chain[-1]), len(records))
    in_chain = {r['uuid'] for r in chain}
    children, answers = {}, {}
    for r in records:
        if r.get('isSidechain') or not isinstance(r.get('uuid'), str) or r['uuid'] in in_chain:
            continue
        parent = r.get('parentUuid')
        if r.get('type') == 'assistant' and isinstance(parent, str):
            children.setdefault(parent, []).append(r)
        elif r.get('type') == 'user' and isinstance(parent, str):
            answers.setdefault(parent, []).append(r)
    def answered(call):
        ids = {b.get('id') for b in blocks(call)}
        found = [r for r in answers.get(call['uuid'], []) if positions[id(r)] < limit]
        return (len(found) == 1 and blocks(found[0]) and all(b.get('type') == 'tool_result' for b in blocks(found[0]))
                and {b.get('tool_use_id') for b in blocks(found[0])} == ids)
    result, taken = [], set()
    for r in chain:
        result.append(r)
        mid = r.get('message', {}).get('id') if r.get('type') == 'assistant' else None
        current = r
        while mid:
            found = [c for c in children.get(current['uuid'], [])
                     if c.get('message', {}).get('id') == mid and c.get('sessionId') == r.get('sessionId')]
            if len(found) != 1:
                break
            call = found[0]
            content = blocks(call)
            if (call['uuid'] in taken or positions[id(call)] > limit or not content
                    or any(b.get('type') != 'tool_use' for b in content) or not answered(call)):
                break
            taken.add(call['uuid'])
            result.append(call)
            current = call
    return result


def _attach_results(chain, records):
    """Pull back the tool_result records of a parallel tool round.

    CC stores parallel tool calls as a branch: each tool_result's parent is its
    own tool_use record, while the leaf path follows the next tool_use. Only
    tool_result-only user records answering an unanswered tool_use in the chain
    are restored, after the assistant run they answer. Everything else off the
    chain stays archived; native verification checks the resulting order.
    """
    unanswered, seen = {}, set()
    for r in chain:
        for b in blocks(r):
            if b.get('type') == 'tool_use':
                ident = b.get('id')
                if not isinstance(ident, str) or ident in seen:
                    raise SlimError('invalid_tool_call')
                seen.add(ident)
                unanswered[ident] = r
            elif b.get('type') == 'tool_result':
                ident = b.get('tool_use_id')
                if not isinstance(ident, str) or ident not in unanswered:
                    raise SlimError('orphan_tool_result')
                unanswered.pop(ident)
    if not unanswered:
        return chain
    positions = {id(r): i for i, r in enumerate(records)}
    in_chain = {r['uuid'] for r in chain}
    extras, matched = {}, set()
    for r in records:
        if r.get('type') != 'user' or r.get('isSidechain') or r.get('uuid') in in_chain:
            continue
        content = blocks(r)
        if not content or any(b.get('type') != 'tool_result' for b in content):
            continue
        ids = [b.get('tool_use_id') for b in content]
        if any(not isinstance(i, str) or i not in unanswered for i in ids):
            continue
        parents = {unanswered[i]['uuid'] for i in ids}
        if parents != {r.get('parentUuid')}:
            continue
        owner = unanswered[ids[0]]
        if r.get('sessionId') != owner.get('sessionId'):
            continue
        if len(set(ids)) != len(ids) or matched.intersection(ids):
            raise SlimError('ambiguous_parallel_tool_result')
        # Never recover a later abandoned branch or an uncommitted tail.
        owner_index = chain.index(owner)
        boundary = next((v for v in chain[owner_index + 1:]
                         if v['type'] != 'assistant' or not any(b.get('type') == 'tool_use' for b in blocks(v))), None)
        following = next((v for v in chain[chain.index(boundary):]
                          if not (v['type'] == 'user' and all(b.get('type') == 'tool_result' for b in blocks(v)))), None) if boundary else None
        end = positions[id(following)] if following else positions[id(chain[-1])] + 1
        if not positions[id(owner)] < positions[id(r)] < end:
            continue
        matched.update(ids)
        extras.setdefault(owner['uuid'], []).append(r)
    result, held = [], []
    def flush():
        # Tool calls remain together; result records retain their native order.
        result.extend(sorted(held, key=lambda r: positions[id(r)]))
        held.clear()
    for r in chain:
        is_result = r['type'] == 'user' and all(b.get('type') == 'tool_result' for b in blocks(r))
        if is_result:
            held.append(r)
        else:
            if (r['type'] != 'assistant' or not any(b.get('type') == 'tool_use' for b in blocks(r))
                    or any(v['uuid'] in in_chain for v in held)):
                flush()
            result.append(r)
        held.extend(extras.get(r['uuid'], []))
    flush()
    validate_tools(result)
    return result


def contains_image(value):
    if isinstance(value, dict):
        return value.get('type') in ('image', 'document') or any(contains_image(v) for v in value.values())
    return isinstance(value, list) and any(contains_image(v) for v in value)


def manifest(records, machine_positions=()):
    result = []
    for index, r in enumerate(records):
        body = [b for block_index, b in enumerate(blocks(r)) if b.get('type') not in ('tool_use', 'tool_result', 'thinking', 'redacted_thinking')
                and (r['uuid'], block_index) not in machine_positions]
        if body:
            result.append({'id': r['uuid'], 'ordinal': index, 'role': r['message']['role'],
                           'body_digest': digest(body), 'block_count': len(body)})
    return result


def validate_tools(records):
    pending, done = {}, set()
    for r in records:
        for b in blocks(r):
            if b.get('type') == 'tool_use':
                ident = b.get('id')
                if not isinstance(ident, str) or ident in pending or ident in done or not isinstance(b.get('input'), dict):
                    raise SlimError('invalid_tool_call')
                pending[ident] = b
            elif b.get('type') == 'tool_result':
                ident = b.get('tool_use_id')
                if not isinstance(ident, str) or ident not in pending:
                    raise SlimError('orphan_tool_result')
                pending.pop(ident)
                done.add(ident)
    if pending:
        raise SlimError('unfinished_tool_round')
    return done


def slim(records, *, machine_positions=(), archive_ref, tool_threshold=1200, parfait=None, completed_tail=False, completed_tool_budget=6000, clear_completed_thinking=False, trim_tool_results=True):
    validate_tools(records)
    before = manifest(records, machine_positions)
    candidate = copy.deepcopy(records)
    removed, tools, thinking, images = 0, 0, 0, 0
    thinking_removed, opaque_bytes_removed = 0, 0
    # The latest real user and its complete tool round remain untouched.
    last_user = max((i for i, r in enumerate(records) if r['type'] == 'user' and
                     any(b.get('type') not in ('tool_result',) for b in blocks(r))), default=0)
    latest_tool_tokens = sum(estimate(b) for r in records[last_user:] for b in blocks(r) if b.get('type') == 'tool_result')
    latest_before = latest_tool_tokens
    names = {b['id']: b.get('name', '') for r in records for b in blocks(r) if b.get('type') == 'tool_use'}
    for i, r in enumerate(candidate):
        revised = []
        for block_index, b in enumerate(blocks(r)):
            kind = b.get('type')
            if kind in ('thinking', 'redacted_thinking') and clear_completed_thinking and completed_tail:
                # Only a DB-confirmed completed stable round enables this. Whole
                # opaque blocks move to the immutable source, never edited.
                thinking_removed += 1
                opaque_bytes_removed += len(json.dumps(b, ensure_ascii=False).encode())
                continue
            if trim_tool_results and kind == 'tool_result' and parfait and not contains_image(b):
                name = names.get(b.get('tool_use_id'), '')
                # Exact MCP tool identity is source metadata, unlike a word in prose.
                if name in ('mcp__companion_brain__get_parfait', 'mcp__brain__get_parfait'):
                    b['content'] = '[完整巴菲由当前系统上下文单份提供；历史返回归档 ' + archive_ref + ']'
                    removed += 1
                elif name in ('mcp__context__context', 'mcp__house_heart__context', 'mcp__house_brain__context', 'mcp__companion_brain__context', 'mcp__brain__context'):
                    b['content'] = '[历史上下文返回已归档 ' + archive_ref + '；当前完整巴菲由系统上下文提供]'
                    removed += 1
            if kind == 'text' and (r['uuid'], block_index) in machine_positions:
                removed += 1
                # Keep a message block (and its boundary) even for machine-only messages.
                revised.append({'type': 'text', 'text': '[历史机器参考资料已移出；原件归档 ' + archive_ref + ']'})
            elif trim_tool_results and kind == 'tool_result' and (i < last_user or completed_tail and latest_tool_tokens > completed_tool_budget) and not contains_image(b) and not has_tool_reference(b) and len(json.dumps(b.get('content', ''), ensure_ascii=False)) > tool_threshold:
                previous = estimate(b)
                b['content'] = tool_receipt(b, archive_ref)
                if i >= last_user: latest_tool_tokens -= previous - estimate(b)
                revised.append(b)
                tools += 1
            else:
                revised.append(b)
            if kind in ('thinking', 'redacted_thinking'):
                thinking += estimate(b)
            if contains_image(b):
                images += 1
        if revised != blocks(r):
            r['message']['content'] = revised
    after = manifest(candidate, machine_positions)
    if before != after:
        raise SlimError('visible_dialogue_changed')
    validate_tools(candidate)
    return candidate, {'visible_manifest_digest': digest(before), 'visible_messages': len(before),
                       'verbatim_verified': True, 'machine_blocks_removed': removed, 'tool_results_trimmed': tools,
                       'thinking_retained_estimate': thinking, 'completed_thinking_blocks_archived': thinking_removed,
                       'opaque_log_bytes_archived': opaque_bytes_removed, 'opaque_bytes_are_model_tokens': False, 'image_blocks_retained': images,
                       'completed_tail_eligible': completed_tail, 'latest_tool_results_before_estimate': latest_before,
                       'latest_tool_results_retained_estimate': latest_tool_tokens, 'completed_tool_budget': completed_tool_budget}


def estimate(value):
    """Explicit heuristic tokens, not CC measurement, bytes, or a tokenizer claim.

    ASCII ~3 characters/token; other Unicode ~1.5 tokens/codepoint. A 15%
    uncertainty allowance and framing overhead are included. Runtime calibration
    is applied separately, to history only, and cannot discount new injections.
    """
    if isinstance(value, dict) and value.get('type') == 'image':
        return 16000  # Explicit conservative image token allowance, not base64 bytes.
    if isinstance(value, dict):
        return sum(estimate(k) + estimate(v) for k, v in value.items())
    if isinstance(value, list):
        return sum(estimate(v) for v in value)
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    ascii_count = sum(ord(c) < 128 for c in value)
    return math.ceil((ascii_count / 3 + (len(value) - ascii_count) * 1.5) * 1.15) + (4 if value else 0)


def request_message(record):
    # Native/API messages carry role and content. Provider usage, model/id,
    # diagnostics and stop metadata in JSONL are not model input.
    message = record['message']
    return {'role': message['role'], 'content': blocks(record)}


def history_estimate(records):
    return sum(estimate(request_message(r)) for r in records)


def history_breakdown(records):
    parts = dict(visible=0, tool_results=0, tool_inputs=0, thinking_allowance=0, other=0)
    for r in records:
        for b in blocks(r):
            key = {'text':'visible','image':'visible','document':'visible','tool_result':'tool_results',
                   'tool_use':'tool_inputs','thinking':'thinking_allowance','redacted_thinking':'thinking_allowance'}.get(b.get('type'),'other')
            parts[key] += estimate(b)
    parts['opaque_log_bytes'] = sum(len(json.dumps(b,ensure_ascii=False).encode()) for r in records for b in blocks(r) if b.get('type') in ('thinking','redacted_thinking'))
    parts['role_and_framing'] = history_estimate(records)-sum(v for k,v in parts.items() if k != 'opaque_log_bytes')
    parts['excluded_log_metadata_estimate'] = sum(estimate(r['message']) for r in records)-history_estimate(records)
    return parts


def has_tool_reference(value):
    if isinstance(value, dict):
        return value.get('type') == 'tool_reference' or any(has_tool_reference(v) for v in value.values())
    return isinstance(value, list) and any(has_tool_reference(v) for v in value)


def tool_receipt(block, archive_ref):
    """Bounded evidence receipt for a completed tool result, not a new summary.

    Keep source identifiers and a labelled excerpt. Full original results remain
    in the immutable native snapshot keyed by archive digest and tool_use_id.
    Tool inputs (including search queries) remain byte-for-byte unchanged.
    """
    content = block.get('content', '')
    refs = []
    def visit(value, depth=0):
        if depth > 12 or len(refs) >= 8: return
        if isinstance(value, str):
            if value.lstrip().startswith(('{','[')):
                try: visit(json.loads(value), depth+1)
                except (ValueError, RecursionError): pass
        elif isinstance(value, list):
            for v in value: visit(v, depth+1)
        elif isinstance(value, dict):
            for k,v in value.items():
                if k.lower().replace('_','') in ('sourceref','sourcerefs','sourceid','memoryid'):
                    for ref in v if isinstance(v, list) else [v]:
                        if isinstance(ref, str) and len(ref) <= 160 and ref not in refs and len(refs)<8: refs.append(ref)
                else: visit(v, depth+1)
    visit(content)
    text = content if isinstance(content, str) else json.dumps(content,ensure_ascii=False,separators=(',',':'))
    return ('[已完成历史工具返回；非完整结果。原件归档 '+archive_ref+'；tool_use_id='+block['tool_use_id']+
            '；sha256='+digest(content)+'；SourceRef='+json.dumps(refs,ensure_ascii=False)+']\n'
            +'[原结果开头摘录，可能截断] '+text[:240])


def serialize(records, session_id, reset_usage=False):
    result, parent = [], None
    for r in records:
        r = copy.deepcopy(r)
        r.update(sessionId=session_id, parentUuid=parent)
        if reset_usage and isinstance(r.get('message', {}).get('usage'), dict):
            def zero(value):
                if isinstance(value, dict): return {k: zero(v) for k, v in value.items()}
                return 0 if isinstance(value, (int, float)) and not isinstance(value, bool) else value
            r['message']['usage'] = zero(r['message']['usage'])
        parent = r['uuid']
        result.append(json.dumps(r, ensure_ascii=False, separators=(',', ':')))
    return ('\n'.join(result) + '\n').encode()


def attachment_estimate(items):
    import chat_attachments
    return sum(estimate(b) for b in chat_attachments.blocks(items, 'claude'))


def synthetic_rate_limit(record, *, stream=False):
    """Identify the native/stream CLI quota marker, never a model response.

    CC 2.1.284 serializes is_api_error_message on stream-json, but
    isApiErrorMessage in native JSONL. Native proofs keep their exact marker.
    """
    marker = record.get('isApiErrorMessage') if isinstance(record, dict) else None
    if stream and isinstance(record, dict) and 'is_api_error_message' in record:
        if marker is not None and marker is not record['is_api_error_message']:
            return None
        marker = record['is_api_error_message']
    if (not isinstance(record, dict) or record.get('type') != 'assistant'
            or marker is not True
            or record.get('error') != 'rate_limit' or record.get('parent_tool_use_id')):
        return None
    message = record.get('message')
    if not isinstance(message, dict) or message.get('model') != '<synthetic>':
        return None
    content = message.get('content')
    if (not isinstance(content, list) or not content
            or any(not isinstance(b, dict) or b.get('type') != 'text'
                   or not isinstance(b.get('text'), str) for b in content)):
        return None
    usage = message.get('usage')
    keys = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] != 0 for k in keys):
        return None
    return ''.join(b['text'] for b in content)[:512]


def terminal_rate_limit_proof(raw, final_text, failed, native_id):
    """Verify a zero-work, terminal quota tail after the exact completed prefix.

    The full source stays archived. Active/unknown/partial/tool-producing tails
    cannot enter this recovery path. The failed user message is never replayed.
    """
    if (not failed or failed.get('native_id') != native_id
            or failed.get('status') != 'failed' or not failed.get('completed_at')
            or failed.get('assistant_text')
            or failed.get('error') not in ('observed_model_mismatch', 'claude_rate_limited')):
        return None
    try:
        receipt = json.loads(failed.get('receipt_json') or '{}')
        if failed['error'] == 'observed_model_mismatch' and receipt.get('observed_model') != '<synthetic>':
            return None
        records = read_records(raw)
        prefix, info = active_chain(records, final_text, True)
        full, _ = active_chain(records)
        n = len(prefix)
        if not n or full[:n] != prefix or len(full) != n + 2 or info.get('excluded_uncommitted_records') != 2:
            return None
        user, error = full[n:]
        if user.get('type') != 'user' or any(r.get('sessionId') != native_id for r in (user,error)):
            return None
        content = blocks(user)
        if (not content or any(b.get('type') != 'text' for b in content)
                or content[-1].get('text') != failed.get('user_text')):
            return None
        detail = synthetic_rate_limit(error)
        if detail is None:
            return None
        leaf_index = next(i for i,r in enumerate(records) if r.get('uuid') == prefix[-1]['uuid'])
        tail_messages = [r for r in records[leaf_index+1:] if r.get('type') in ('user','assistant')]
        if tail_messages != [user,error]:
            return None
        from datetime import datetime
        for r in (user,error):
            when = datetime.fromisoformat(r['timestamp'].replace('Z','+00:00')).timestamp()
            if not failed['created_at'] - 1 <= when <= failed['completed_at'] + 1:
                return None
        return {'kind':'terminal_cli_rate_limit', 'client_message_id':failed['client_message_id'],
                'native_id':native_id, 'completed_at':failed['completed_at'],
                'source_digest':digest(raw.hex()), 'retired_records':2,
                'upstream_notice':detail, 'original_failed_turn_preserved':True,
                'automatic_replay':False}
    except (SlimError, ValueError, TypeError, KeyError, StopIteration):
        return None


# turn_outcome_unknown: startup reconciliation of a turn that was still running
# when the adapter/host died (10-27 19:03 power loss). It never got completed_at.
NO_TRACE_ERRORS = ('claude_adapter_error', 'turn_outcome_unknown')


def terminal_no_trace_proof(raw, final_text, failed, native_id, registered_tools):
    """Verify a failed turn that never reached the native transcript.

    The committed final must still be the last message, nothing may follow it,
    and no record may be stamped at or after the failed turn's start. A turn
    with any registered tool, text, or guard stop cannot enter this path. The
    failed user message is never replayed.
    """
    if (not failed or failed.get('native_id') != native_id
            or failed.get('status') != 'failed' or failed.get('error') not in NO_TRACE_ERRORS
            or failed.get('assistant_text') or failed.get('guard_error') or registered_tools):
        return None
    try:
        started = float(failed['created_at'])
        if failed.get('completed_at') is None and failed['error'] == 'turn_outcome_unknown':
            ended = started  # Startup marks it failed without a completion time.
        else:
            ended = float(failed['completed_at'])
        if not (math.isfinite(started) and math.isfinite(ended) and started <= ended):
            return None
        receipt = json.loads(failed.get('receipt_json') or '{}')
        if receipt.get('observed_model') is not None or receipt.get('cli_model') is not None:
            return None
        records = read_records(raw)
        prefix, info = active_chain(records, final_text, True)
        full, _ = active_chain(records)
        if not prefix or full != prefix or info.get('excluded_uncommitted_records'):
            return None
        leaf_index = next(i for i,r in enumerate(records) if r.get('uuid') == prefix[-1]['uuid'])
        if any(r.get('type') in ('user','assistant') for r in records[leaf_index+1:]):
            return None
        from datetime import datetime
        latest = None
        for r in records:
            stamp = r.get('timestamp')
            if stamp is None:
                continue
            when = datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()
            if when >= started - 1:
                return None
            latest = when if latest is None else max(latest, when)
        return {'kind':'terminal_no_trace', 'client_message_id':failed['client_message_id'],
                'native_id':native_id, 'completed_at':ended, 'failed_error':failed['error'],
                'source_digest':digest(raw.hex()), 'retired_records':0,
                'latest_record_at':latest, 'original_failed_turn_preserved':True,
                'automatic_replay':False}
    except (SlimError, ValueError, TypeError, KeyError, StopIteration, AttributeError):
        return None


GUARD_STOPS = ('tool_repeat_limit', 'tool_call_limit')


def terminal_guard_proof(raw, final_text, failed, native_id, registered_tool_ids, registered_tools=None):
    """Verify a turn the local tool guard stopped, after the exact completed prefix.

    Unlike a quota stop this tail did real tool work, so its effects are unknown.
    Every tool call in the tail must be one the guard registered for this turn,
    apart from at most one call the guard refused with an error result, and the
    tail must hold nothing else. It stays archived and is never replayed.
    """
    reason = failed.get('error') if failed else None
    if (not failed or failed.get('native_id') != native_id
            or failed.get('status') != 'failed' or not failed.get('completed_at')
            or failed.get('assistant_text')
            or reason not in GUARD_STOPS or failed.get('guard_error') != reason):
        return None
    proof = _terminal_tool_tail_proof(raw, final_text, failed, native_id, registered_tool_ids, reason)
    if proof or registered_tools is None:
        return proof
    return _terminal_parallel_guard_proof(raw, final_text, failed, native_id,
                                          registered_tool_ids, registered_tools, reason)


def _terminal_parallel_guard_proof(raw, final_text, failed, native_id, registered_tool_ids, registered_tools, reason):
    """Prove a complete interrupted tool graph, including the denied sibling.

    The native interruption leaf can follow the cancelled sibling instead of
    the denied call. Read every tail message and prove its ancestry and tools;
    never add these messages to the committed chain or replay their effects.
    """
    try:
        from datetime import datetime
        from .accounting import model_matches
        records = read_records(raw)
        prefix, info = active_chain(records, final_text, True)
        if not prefix:
            return None
        anchor = prefix[-1]['uuid']
        leaf_index = next(i for i, r in enumerate(records) if r.get('uuid') == anchor)
        after = records[leaf_index + 1:]
        tail = [r for r in after if r.get('type') in ('user', 'assistant')]
        if (len(tail) < 5 or info.get('excluded_uncommitted_records') != len(tail)
                or any(r.get('sessionId') != native_id or r.get('isSidechain') for r in after)):
            return None
        ids = [r['uuid'] for r in records if 'uuid' in r]
        if any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids):
            return None
        user, marker = tail[0], tail[-1]
        content = blocks(user)
        prompt = user.get('promptId')
        if (user.get('type') != 'user' or not content
                or any(b.get('type') != 'text' for b in content)
                or content[-1].get('text') != failed.get('user_text')
                or not isinstance(prompt, str) or not prompt
                or user.get('parentUuid') != anchor
                or marker.get('type') != 'user' or marker.get('promptId') != prompt
                or blocks(marker) != [{'type': 'text', 'text': '[Request interrupted by user for tool use]'}]):
            return None
        # All branches must descend from this one failed user turn. Attachment
        # nodes may connect messages but cannot introduce another message root.
        nodes = {r['uuid']: r for r in after if 'uuid' in r}
        connected = {user['uuid']}
        for r in nodes.values():
            if r.get('type') not in ('user', 'assistant', 'attachment'):
                return None
            when = datetime.fromisoformat(r['timestamp'].replace('Z', '+00:00')).timestamp()
            if not failed['created_at'] - 1 <= when <= failed['completed_at'] + 1:
                return None
            current, seen = r, set()
            while current['uuid'] not in connected:
                if current['uuid'] in seen:
                    return None
                seen.add(current['uuid'])
                current = nodes[current['parentUuid']]
            connected.update(seen)
        for r in tail[1:-1]:
            if r['type'] == 'user':
                if r.get('promptId') != prompt or not blocks(r) or any(b.get('type') != 'tool_result' for b in blocks(r)):
                    return None
            elif (not model_matches(failed.get('model'), r.get('message', {}).get('model'))
                    or any(b.get('type') not in ('text', 'thinking', 'redacted_thinking', 'tool_use') for b in blocks(r))):
                return None
        validate_tools(tail)
        calls = {b['id']: (r, b) for r in tail for b in blocks(r) if b.get('type') == 'tool_use'}
        results = {b['tool_use_id']: (r, b) for r in tail for b in blocks(r) if b.get('type') == 'tool_result'}
        tools = [dict(t) for t in registered_tools]
        registered = {t['tool_id'] for t in tools}
        if (not tools or len(registered) != len(tools) or registered != set(registered_tool_ids)
                or not registered <= calls.keys() or len(calls.keys() - registered) != 1):
            return None
        for t in tools:
            call = calls[t['tool_id']][1]
            fingerprint = hashlib.sha256(json.dumps(
                [call.get('name'), call.get('input')], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if (t.get('session_id') != failed.get('session_id')
                    or t.get('client_message_id') != failed.get('client_message_id')
                    or t.get('name') != call.get('name') or t.get('fingerprint') != fingerprint):
                return None
        for ident, (r, result) in results.items():
            owner = calls[ident][0]
            if r.get('parentUuid') != owner['uuid'] or r.get('sourceToolAssistantUUID') != owner['uuid']:
                return None
        denied = next(iter(calls.keys() - registered))
        denial_record, denial = results[denied]
        if (denial_record.get('toolDenialKind') != 'permission-rule'
                or denial.get('is_error') is not True
                or denial.get('content') != 'PreToolUse:' + calls[denied][1]['name'] + ' hook error: ' + reason):
            return None
        cancelled = nodes[marker['parentUuid']]
        cancelled_blocks = blocks(cancelled)
        if (cancelled.get('toolDenialKind') != 'user-rejected' or len(cancelled_blocks) != 1
                or cancelled_blocks[0].get('type') != 'tool_result'
                or cancelled_blocks[0].get('tool_use_id') not in registered
                or cancelled_blocks[0].get('is_error') is not True):
            return None
        cancelled_owner = calls[cancelled_blocks[0]['tool_use_id']][0]
        denied_owner = calls[denied][0]
        if cancelled_owner.get('message', {}).get('id') != denied_owner.get('message', {}).get('id') or not denied_owner.get('message', {}).get('id'):
            return None
        return {'kind': 'terminal_parallel_tool_guard_stop', 'guard_reason': reason,
                'client_message_id': failed['client_message_id'], 'native_id': native_id,
                'completed_at': failed['completed_at'], 'source_digest': digest(raw.hex()),
                'retired_records': len(tail), 'registered_tool_calls': len(registered),
                'refused_tool_calls': 1, 'tool_fingerprints_verified': True,
                'terminal_marker_id': marker['uuid'], 'tool_effects': 'unknown_not_replayed',
                'original_failed_turn_preserved': True, 'automatic_replay': False}
    except (SlimError, ValueError, TypeError, KeyError, StopIteration, AttributeError):
        return None


def _terminal_tool_tail_proof(raw, final_text, failed, native_id, registered_tool_ids, reason, allow_refused=True):
    try:
        records = read_records(raw)
        prefix, info = active_chain(records, final_text, True)
        full, _ = active_chain(records)
        n = len(prefix)
        if not n or full[:n] != prefix or len(full) == n:
            return None
        tail = full[n:]
        leaf_index = next(i for i,r in enumerate(records) if r.get('uuid') == prefix[-1]['uuid'])
        after = records[leaf_index+1:]
        tail_messages = [r for r in after if r.get('type') in ('user','assistant')]
        if (info.get('excluded_uncommitted_records') != len(tail) or len(tail_messages) != len(tail)
                or {id(r) for r in tail_messages} != {id(r) for r in tail}
                or any(r.get('sessionId') != native_id for r in after)):
            return None
        user = tail[0]
        content = blocks(user)
        if (user.get('type') != 'user' or not content or any(b.get('type') != 'text' for b in content)
                or content[-1].get('text') != failed.get('user_text')):
            return None
        from datetime import datetime
        for r in tail:
            when = datetime.fromisoformat(r['timestamp'].replace('Z','+00:00')).timestamp()
            if not failed['created_at'] - 1 <= when <= failed['completed_at'] + 1:
                return None
        uses, results = {}, {}
        for r in tail:
            for b in blocks(r):
                if b.get('type') == 'tool_use':
                    uses[b.get('id')] = b
                elif b.get('type') == 'tool_result':
                    results[b.get('tool_use_id')] = b
        registered = set(registered_tool_ids)
        refused = set(uses) - registered
        if not registered <= set(uses) or len(refused) > (1 if allow_refused else 0):
            return None
        for ident in refused:
            result = results.get(ident) or {}
            body = result.get('content')
            if isinstance(body, list):
                body = ''.join(b.get('text', '') for b in body if isinstance(b, dict))
            if not result.get('is_error') or not isinstance(body, str) or reason not in body:
                return None
        return {'kind':'terminal_tool_guard_stop', 'guard_reason':reason,
                'client_message_id':failed['client_message_id'], 'native_id':native_id,
                'completed_at':failed['completed_at'], 'source_digest':digest(raw.hex()),
                'retired_records':len(tail), 'registered_tool_calls':len(registered),
                'refused_tool_calls':len(refused), 'tool_effects':'unknown_not_replayed',
                'original_failed_turn_preserved':True, 'automatic_replay':False}
    except (SlimError, ValueError, TypeError, KeyError, StopIteration, AttributeError):
        return None


def terminal_step_limit_proof(raw, final_text, failed, native_id, registered_tool_ids, step_limit):
    """Prove a closed CLI max-turns tail; preserve its source without replay."""
    if (not failed or failed.get('native_id') != native_id
            or failed.get('status') != 'failed' or not failed.get('completed_at')
            or failed.get('assistant_text') or failed.get('guard_error')
            or failed.get('error') not in ('claude_process_failed', 'api_step_limit')
            or type(step_limit) is not int or step_limit < 1):
        return None
    proof = _terminal_tool_tail_proof(raw, final_text, failed, native_id,
                                     registered_tool_ids, 'api_step_limit', allow_refused=False)
    if not proof:
        return None
    try:
        records = read_records(raw)
        full, _ = active_chain(records)
        last = full[-1]
        last_index = next(i for i,r in enumerate(records) if r.get('uuid') == last['uuid'])
        markers = [r for r in records[last_index+1:]
                   if r.get('type') == 'attachment'
                   and r.get('attachment', {}).get('type') == 'max_turns_reached']
        if len(markers) != 1:
            return None
        marker = markers[0]
        detail = marker['attachment']
        if (marker.get('isSidechain') or marker.get('sessionId') != native_id
                or type(detail.get('maxTurns')) is not int or detail['maxTurns'] != step_limit
                or type(detail.get('turnCount')) is not int or detail['turnCount'] != step_limit + 1):
            return None
        from datetime import datetime
        when = datetime.fromisoformat(marker['timestamp'].replace('Z','+00:00')).timestamp()
        if not failed['created_at'] - 1 <= when <= failed['completed_at'] + 1:
            return None
        # Reject a marker on another branch, even if its session/time match.
        nodes = {r.get('uuid'): r for r in records if r.get('uuid')}
        node, seen = marker, set()
        while node.get('uuid') != last['uuid']:
            if (node.get('uuid') in seen or node.get('type') != 'attachment'
                    or node.get('sessionId') != native_id or node.get('isSidechain')):
                return None
            seen.add(node.get('uuid'))
            node = nodes[node['parentUuid']]
        proof.pop('guard_reason')
        proof.update(kind='terminal_cli_step_limit', step_limit=step_limit,
                     terminal_marker_id=marker['uuid'])
        return proof
    except (SlimError, ValueError, TypeError, KeyError, StopIteration, AttributeError):
        return None


def terminal_worked_rate_limit_proof(raw, final_text, failed, native_id, registered_tools):
    """Prove a quota stop after tool work without replaying the failed turn.

    Require the exact native quota marker, source turn/time boundary, complete
    paired tools, and the independent guard's names and argument fingerprints.
    A true provider model mismatch cannot enter this branch.
    """
    if (not failed or failed.get('native_id') != native_id
            or failed.get('status') != 'failed' or not failed.get('completed_at')
            or failed.get('assistant_text') or failed.get('guard_error')
            or failed.get('error') not in ('observed_model_mismatch', 'claude_rate_limited')):
        return None
    try:
        receipt = json.loads(failed.get('receipt_json') or '{}')
        if (failed['error'] == 'observed_model_mismatch'
                and receipt.get('observed_model') != '<synthetic>'):
            return None
        tools = [dict(t) for t in registered_tools]
        if (not tools or len({t['tool_id'] for t in tools}) != len(tools)
                or any(t.get('session_id') != failed.get('session_id')
                       or t.get('client_message_id') != failed.get('client_message_id') for t in tools)):
            return None
        proof = _terminal_tool_tail_proof(raw, final_text, failed, native_id,
                                         [t['tool_id'] for t in tools],
                                         'claude_rate_limited', allow_refused=False)
        if not proof:
            return None
        records = read_records(raw)
        prefix, _ = active_chain(records, final_text, True)
        full, _ = active_chain(records)
        tail = full[len(prefix):]
        last = tail[-1]
        detail = synthetic_rate_limit(last)
        if detail is None:
            return None
        # Quota is a unique terminal native marker, not ordinary assistant text.
        if sum(r.get('isApiErrorMessage') is True for r in tail) != 1:
            return None
        from .accounting import model_matches
        for record in tail[:-1]:
            if record['type'] == 'assistant':
                if not model_matches(failed.get('model'), record.get('message', {}).get('model')):
                    return None
            elif record is not tail[0] and any(b.get('type') != 'tool_result' for b in blocks(record)):
                return None  # No additional unregistered user/companion turn.
        validate_tools(tail)
        calls = {b['id']: b for record in tail for b in blocks(record) if b.get('type') == 'tool_use'}
        for tool in tools:
            call = calls[tool['tool_id']]
            fingerprint = hashlib.sha256(json.dumps(
                [call.get('name'), call.get('input')], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if tool.get('name') != call.get('name') or tool.get('fingerprint') != fingerprint:
                return None
        proof.pop('guard_reason')
        proof.update(kind='terminal_cli_rate_limit_after_tools',
                     terminal_marker_id=last['uuid'], upstream_notice=detail,
                     tool_fingerprints_verified=True)
        return proof
    except (SlimError, ValueError, TypeError, KeyError, StopIteration, AttributeError):
        return None
