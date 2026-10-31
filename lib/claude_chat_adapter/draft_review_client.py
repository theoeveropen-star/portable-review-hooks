"""Private, once-per-call reviewer transport; the host owns durable attempts.

No endpoint/config/model selection is read from a model's prompt or output.
The caller must reserve its persistent turn attempt before invoking this
function. A timeout/cancellation does not reopen that attempt. This transport
does not retrieve history, revise prose, write storage, or approve delivery.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
from outbound_proxy import https_connection

from . import draft_review_contract as contract


VERSION = 'companion-private-draft-review-client/v1'
PROMPT_SHA256 = '23fd61a3605d8c711e3de9f174112e4108c6fab69f615fcf9036d8d6da8406e4'
MAX_DEADLINE_SECONDS = 20.0
MAX_RESPONSE_BYTES = 131072
_INFLIGHT = threading.BoundedSemaphore(2)
_PROTOCOLS = {'openai': 'openai', 'openai_chat_completions': 'openai',
              'anthropic': 'anthropic', 'anthropic_messages': 'anthropic'}


class ReviewerError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderSnapshot:
    key: str = field(repr=False)
    base_url: str = field(repr=False)
    model: str
    protocol: str
    current_user_sha256: str | None = field(default=None, repr=False)
    source_binding_sha256: str | None = field(default=None, repr=False)
    cancel_event: object = field(default=None, repr=False, compare=False)
    thinking: str | None = None
    reasoning_effort: str | None = None


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _contains_secret(value, key):
    if isinstance(value, str):
        return key in value
    if isinstance(value, list):
        return any(_contains_secret(item, key) for item in value)
    if isinstance(value, dict):
        return any(_contains_secret(name, key) or _contains_secret(item, key) for name, item in value.items())
    return False


def _snapshot(value):
    if isinstance(value, ProviderSnapshot):
        snap = ProviderSnapshot(value.key, value.base_url, value.model, value.protocol,
                                value.current_user_sha256, value.source_binding_sha256, value.cancel_event,
                                thinking=value.thinking, reasoning_effort=value.reasoning_effort)
    elif (isinstance(value, dict) and set(value) in ({'key', 'base_url', 'model', 'protocol'},
            {'key', 'base_url', 'model', 'protocol', 'thinking'},
            {'key', 'base_url', 'model', 'protocol', 'reasoning_effort'})
            and ('thinking' not in value or value['thinking'] == 'disabled')
            and ('reasoning_effort' not in value or value['reasoning_effort'] == 'low')):
        snap = ProviderSnapshot(**dict(value))
    else:
        raise ReviewerError('reviewer_provider_snapshot_invalid')
    if (not isinstance(snap.key, str) or not 8 <= len(snap.key) <= 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in snap.key)
            or not isinstance(snap.base_url, str) or len(snap.base_url) > 2048
            or not isinstance(snap.model, str) or not snap.model or snap.model != snap.model.strip()
            or snap.model.startswith('-') or len(snap.model.encode('utf-8')) > 512
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in snap.model)
            or snap.key in snap.model or snap.key in snap.base_url
            or not isinstance(snap.protocol, str) or snap.protocol not in _PROTOCOLS
            or snap.thinking is not None and (snap.thinking != 'disabled' or _PROTOCOLS[snap.protocol] != 'openai')
            or snap.reasoning_effort is not None and (snap.reasoning_effort != 'low'
                or _PROTOCOLS[snap.protocol] != 'openai' or snap.thinking is not None)):
        raise ReviewerError('reviewer_provider_snapshot_invalid')
    return ProviderSnapshot(snap.key, snap.base_url, snap.model, _PROTOCOLS[snap.protocol],
                            snap.current_user_sha256, snap.source_binding_sha256, snap.cancel_event,
                            thinking=snap.thinking, reasoning_effort=snap.reasoning_effort)


def snapshot_from_claude(adapter, session_id, turn_id, reviewer_config):
    """Read actual active-turn/provider state under the existing adapter lock.

    reviewer_config is a private host-owned mapping containing model, protocol
    and enabled_sessions (plus optional enabled and one fixed reasoning control).
    It is never accepted from tool/model input. No wildcard session enable or
    credential override exists.
    """
    if (not isinstance(reviewer_config, dict)
            or set(reviewer_config) - {'enabled', 'model', 'protocol', 'enabled_sessions', 'thinking', 'reasoning_effort'}
            or not {'model', 'protocol', 'enabled_sessions'} <= set(reviewer_config)
            or 'thinking' in reviewer_config and 'reasoning_effort' in reviewer_config
            or 'thinking' in reviewer_config and (reviewer_config['thinking'] != 'disabled'
                or reviewer_config['protocol'] != 'openai')
            or 'reasoning_effort' in reviewer_config and (reviewer_config['reasoning_effort'] != 'low'
                or reviewer_config['protocol'] != 'openai')
            or not isinstance(reviewer_config['enabled_sessions'], list)
            or any(not isinstance(item, str) or not item for item in reviewer_config['enabled_sessions'])):
        raise ReviewerError('reviewer_private_config_invalid')
    config = {**reviewer_config, 'enabled_sessions': list(reviewer_config['enabled_sessions'])}
    if config.get('enabled', True) is not True or session_id not in config['enabled_sessions']:
        raise ReviewerError('reviewer_not_enabled_for_session')
    with adapter.lock:
        if getattr(adapter.api, 'a', None) is not adapter:
            raise ReviewerError('reviewer_provider_source_unbound')
        live = adapter.db.execute('SELECT status,user_text FROM claude_turns WHERE session_id=? AND client_message_id=?',
                                  (session_id, turn_id)).fetchone()
        active = adapter.active.get((session_id, turn_id))
        if not live or live[0] != 'running' or not active or active['cancel'].is_set():
            raise ReviewerError('reviewer_turn_inactive')
        settings = adapter.api.settings
        snap = ProviderSnapshot(settings.get('key'), settings.get('base_url'), config['model'], config['protocol'],
                                current_user_sha256=_sha(live[1]),
                                source_binding_sha256=_sha(_json([session_id, turn_id, live[1]])),
                                cancel_event=active['cancel'], thinking=config.get('thinking'),
                                reasoning_effort=config.get('reasoning_effort'))
    return _snapshot(snap)


def _public_target(base_url):
    # Prefer the installed validator. Equivalent fallback keeps pure local
    # transport tests and package-only imports independent of Channel creation.
    try:
        from .api_channel import public_target
    except ImportError:
        value = base_url.strip().rstrip('/')
        value = value[:-3] if value.endswith('/v1') else value
        parsed = urlsplit(value or 'https://api.anthropic.com')
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
            raise ReviewerError('reviewer_provider_origin_invalid')
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ReviewerError('reviewer_provider_origin_invalid')
        return parsed
    return public_target(base_url)


def _numeric_usage(value):
    if not isinstance(value, dict):
        return {}
    allowed = {'input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens', 'total_tokens',
               'cache_creation_input_tokens', 'cache_read_input_tokens', 'cached_tokens',
               'reasoning_tokens', 'thinking_tokens', 'audio_tokens', 'text_tokens',
               'accepted_prediction_tokens', 'rejected_prediction_tokens'}
    result = {}
    for key, item in value.items():
        if key in allowed and type(item) in (int, float) and math.isfinite(item) and item >= 0:
            result[key] = item
        elif key in {'prompt_tokens_details', 'completion_tokens_details', 'output_tokens_details',
                     'input_tokens_details', 'cache_creation'} and isinstance(item, dict):
            nested = _numeric_usage(item)
            if nested:
                result[key] = nested
    return result


def _build_payload(bounded_input, snap, prompt):
    common = {'model': snap.model, 'temperature': 0, 'stream': False}
    user = {'role': 'user', 'content': _json(bounded_input)}
    if snap.protocol == 'openai':
        common.update(max_tokens=2048, messages=[{'role': 'system', 'content': prompt}, user],
                      response_format={'type': 'json_object'})
        if snap.thinking == 'disabled':
            common['thinking'] = {'type': 'disabled'}
        if snap.reasoning_effort == 'low':
            common['reasoning_effort'] = 'low'
        return '/v1/chat/completions', common
    common.pop('temperature')  # Some Anthropic-compatible models reject this optional field.
    common.update(max_tokens=1024, system=prompt, messages=[user])
    return '/v1/messages', common


def _model_text(envelope, protocol):
    if not isinstance(envelope, dict):
        raise ReviewerError('reviewer_response_envelope_invalid')
    if protocol == 'openai':
        choices = envelope.get('choices')
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ReviewerError('reviewer_response_envelope_invalid')
        message = choices[0].get('message')
        if (not isinstance(message, dict) or not isinstance(message.get('content'), str)
                or message.get('tool_calls') or message.get('function_call')):
            raise ReviewerError('reviewer_response_envelope_invalid')
        stop = choices[0].get('finish_reason')
        if stop == 'length':
            raise ReviewerError('reviewer_output_truncated')
        if stop != 'stop':
            raise ReviewerError('reviewer_response_incomplete')
        return message['content'], stop
    blocks = envelope.get('content')
    stop = envelope.get('stop_reason')
    if stop == 'max_tokens':
        raise ReviewerError('reviewer_output_truncated')
    if stop != 'end_turn' or not isinstance(blocks, list):
        raise ReviewerError('reviewer_response_envelope_invalid')
    texts = []
    for item in blocks:
        if not isinstance(item, dict):
            raise ReviewerError('reviewer_response_envelope_invalid')
        kind = item.get('type')
        if kind == 'text' and isinstance(item.get('text'), str):
            texts.append(item['text'])
        elif (kind == 'thinking' and set(item) == {'type', 'thinking', 'signature'}
                and isinstance(item['thinking'], str) and isinstance(item['signature'], str)):
            continue
        elif (kind == 'redacted_thinking' and set(item) == {'type', 'data'}
                and isinstance(item['data'], str)):
            continue
        else:
            raise ReviewerError('reviewer_response_envelope_invalid')
    if not texts:
        raise ReviewerError('reviewer_response_envelope_invalid')
    return '\n'.join(texts), stop


def classify_once(input, provider_snapshot, cancel_event=None, deadline=None):
    """One bounded provider attempt. `deadline` is an absolute monotonic time.

    `input` is the complete bounded buildDraftReviewInput result. A successful
    plan is syntax/reference validation, not semantic factual approval. This
    call has no retries or persistent deduplication; the host owns that ledger.
    """
    started = time.monotonic()
    result = {'version': VERSION, 'status': 'unavailable', 'plan': None, 'work': None, 'usage': {},
              'metadata': {'provider_request_attempts': 0, 'completion_unknown': False},
              'approval_provided': False, 'storage_performed': False, 'delivery_confirmed': False}
    metadata = result['metadata']

    def finish(status=None, error=None):
        if status:
            result['status'] = status
        if error:
            result['error'] = error
        metadata['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
        return result

    try:
        # JSON clone/validate happens synchronously, before yielding control to
        # an I/O worker. A caller mutation cannot alter this request or binding.
        contract.validateDraftReviewPlan({'route': 'pass', 'issues': []}, input)
        bounded = json.loads(_json(input))
        snap = _snapshot(provider_snapshot)
        if snap.current_user_sha256 is not None and _sha(bounded['current_user']['content']) != snap.current_user_sha256:
            raise ReviewerError('reviewer_current_user_binding_mismatch')
        if deadline is None:
            deadline = started + MAX_DEADLINE_SECONDS
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ReviewerError('reviewer_deadline_invalid')
        deadline = min(deadline, started + MAX_DEADLINE_SECONDS)
        for event in (cancel_event, snap.cancel_event):
            if event is not None and not callable(getattr(event, 'is_set', None)):
                raise ReviewerError('reviewer_cancel_event_invalid')
        prompt_bytes = Path(__file__).with_name('draft-review-instruction.txt').read_bytes()
        if hashlib.sha256(prompt_bytes).hexdigest() != PROMPT_SHA256:
            raise ReviewerError('reviewer_prompt_source_changed')
        prompt = prompt_bytes.decode('utf-8')
        route, payload = _build_payload(bounded, snap, prompt)
        body = _json(payload).encode('utf-8')
        if _contains_secret(bounded, snap.key) or snap.key in prompt or snap.key.encode() in body:
            raise ReviewerError('reviewer_credential_in_prompt')
        metadata.update(protocol=snap.protocol, model_requested=snap.model,
                        prompt_sha256=PROMPT_SHA256, request_bytes=len(body),
                        request_sha256=hashlib.sha256(body).hexdigest(), input_sha256=_sha(_json(bounded)))
        if snap.thinking == 'disabled':
            metadata['thinking_mode'] = 'disabled'
        if snap.reasoning_effort == 'low':
            metadata['reasoning_effort'] = 'low'
        if snap.source_binding_sha256 is not None:
            metadata['source_binding_sha256'] = snap.source_binding_sha256
    except contract.DraftReviewContractError as error:
        result['contract_error'] = error.code
        return finish('rejected', 'reviewer_input_invalid')
    except ReviewerError as error:
        return finish('rejected', str(error))
    except Exception:
        return finish('rejected', 'reviewer_input_or_configuration_unavailable')

    abort = threading.Event()
    done = threading.Event()
    state_lock = threading.Lock()
    state = {'connection': None, 'attempted': False, 'response_complete': False, 'outcome': None}

    def cancelled():
        return abort.is_set() or any(event is not None and event.is_set() for event in (cancel_event, snap.cancel_event))

    def remaining():
        if cancelled():
            raise ReviewerError('reviewer_cancelled')
        left = deadline - time.monotonic()
        if left <= 0:
            raise ReviewerError('reviewer_deadline')
        return left

    try:
        remaining()
    except ReviewerError as error:
        return finish('unavailable', str(error))
    if not _INFLIGHT.acquire(blocking=False):
        return finish('unavailable', 'reviewer_client_capacity')

    def close_connection():
        with state_lock:
            connection = state['connection']
        if connection is not None:
            sock = getattr(connection, 'sock', None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
            try:
                connection.close()
            except Exception:
                pass

    def worker():
        outcome = {'status': 'unavailable', 'plan': None, 'work': None, 'usage': {}, 'metadata': {}}
        try:
            remaining()
            target = _public_target(snap.base_url)
            connection = https_connection(target.hostname, 443, timeout=remaining(), context=ssl.create_default_context())
            connection.set_debuglevel(0)
            with state_lock:
                state['connection'] = connection
            remaining()
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Authorization': 'Bearer ' + snap.key}
            if snap.protocol == 'anthropic':
                headers.update({'x-api-key': snap.key, 'anthropic-version': '2023-06-01'})
            with state_lock:
                remaining()
                state['attempted'] = True
            connection.request('POST', target.path.rstrip('/') + route, body=body, headers=headers)
            if connection.sock is not None:
                connection.sock.settimeout(remaining())
            response = connection.getresponse()
            outcome['metadata']['http_status'] = response.status
            chunks, size = [], 0
            while True:
                if connection.sock is not None:
                    connection.sock.settimeout(remaining())
                else:
                    remaining()
                part = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                if not part:
                    break
                chunks.append(part)
                size += len(part)
                if size > MAX_RESPONSE_BYTES:
                    raise ReviewerError('reviewer_response_too_large')
            raw = b''.join(chunks)
            with state_lock:
                state['response_complete'] = True
            outcome['metadata'].update(response_bytes=len(raw), response_sha256=hashlib.sha256(raw).hexdigest())
            if snap.key.encode() in raw:
                raise ReviewerError('reviewer_credential_reflected')
            if response.status != 200:
                raise ReviewerError('reviewer_provider_http_rejected')
            try:
                envelope = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ReviewerError('reviewer_response_not_json') from None
            if _contains_secret(envelope, snap.key):
                raise ReviewerError('reviewer_credential_reflected')
            if isinstance(envelope, dict):
                outcome['usage'] = _numeric_usage(envelope.get('usage'))
                observed = envelope.get('model')
                if (isinstance(observed, str) and len(observed.encode('utf-8')) <= 512
                        and not any(ord(char) < 32 for char in observed)):
                    outcome['metadata']['model_returned'] = observed
            model_text, stop = _model_text(envelope, snap.protocol)
            outcome['metadata']['stop_reason'] = stop
            plan = contract.validateDraftReviewPlan(model_text, bounded)
            work = contract.buildReviewWork(plan, bounded)
            remaining()  # A valid response received after cancellation is not accepted.
            outcome.update(status='ok', plan=plan, work=work)
        except contract.DraftReviewContractError as error:
            outcome.update(error='reviewer_plan_invalid', contract_error=error.code)
        except ReviewerError as error:
            outcome['error'] = str(error)
        except (TimeoutError, socket.timeout):
            outcome['error'] = 'reviewer_deadline'
        except Exception:
            outcome['error'] = 'reviewer_transport_unavailable'
        finally:
            close_connection()
            with state_lock:
                state['outcome'] = outcome
            _INFLIGHT.release()
            done.set()

    thread = threading.Thread(target=worker, name='private-draft-reviewer', daemon=True)
    try:
        thread.start()
    except Exception:
        _INFLIGHT.release()
        return finish('unavailable', 'reviewer_worker_unavailable')
    while not done.is_set():
        if cancelled() or time.monotonic() >= deadline:
            reason = 'reviewer_cancelled' if cancelled() else 'reviewer_deadline'
            abort.set()
            close_connection()
            with state_lock:
                metadata['provider_request_attempts'] = int(state['attempted'])
                metadata['completion_unknown'] = state['attempted'] and not state['response_complete']
            return finish('unavailable', reason)
        done.wait(min(0.025, max(0, deadline - time.monotonic())))
    with state_lock:
        outcome = state['outcome']
        metadata['provider_request_attempts'] = int(state['attempted'])
        metadata['completion_unknown'] = state['attempted'] and not state['response_complete']
    if cancelled() or time.monotonic() >= deadline:
        return finish('unavailable', 'reviewer_cancelled' if cancelled() else 'reviewer_deadline')
    result.update({key: value for key, value in outcome.items() if key != 'metadata'})
    metadata.update(outcome['metadata'])
    return finish()
