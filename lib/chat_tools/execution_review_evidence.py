"""Isolated candidate receipt ingress, never a public or production capability."""
import hashlib
import hmac
import json
import os
import re

VERSION = 'companion-review-evidence/v1'

def verify_execution_receipt(value, window, turn):
    key = os.environ.get('ANTI_HALLUCINATION_RECEIPT_KEY', '')
    if (not isinstance(value, dict) or not re.fullmatch('[a-f0-9]{64}', key) or not isinstance(window, str) or not window
            or not isinstance(turn, str) or not turn
            or value.get('kind') != 'isolated_bound_execution'
            or not isinstance(value.get('signed_payload'), str)
            or not re.fullmatch('[a-f0-9]{64}', str(value.get('signature', '')))):
        return None
    expected = hmac.new(bytes.fromhex(key), value['signed_payload'].encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, value['signature']):
        return None
    try:
        bound = json.loads(value['signed_payload']); receipt = bound.get('receipt')
        if (bound.get('version') != 'isolated-bound-execution/v1' or bound.get('window') != window
                or bound.get('turn') != turn or bound.get('execution_verified') is not True
                or not isinstance(receipt, dict) or bound.get('tool_call_id') != receipt.get('call_id')
                or not isinstance(bound.get('tool_call_id'), str) or not bound['tool_call_id']
                or not re.fullmatch('[a-f0-9]{64}', str(bound.get('execution_sha256', '')))
                or receipt.get('version') != 'isolated-tool-receipt/v1' or receipt.get('fixture') is not True
                or not isinstance(receipt.get('tool_name'), str) or not receipt['tool_name']
                or not isinstance(receipt.get('observed_at'), str) or not receipt['observed_at']
                or not isinstance(receipt.get('result') if receipt.get('status') == 'success' else receipt.get('error'), dict)
                or bound.get('status') != receipt.get('status') or bound.get('status') not in ('success', 'error', 'unavailable')
                or not isinstance(bound.get('receipt_json'), str)
                or hashlib.sha256(bound['receipt_json'].encode()).hexdigest() != bound.get('receipt_sha256')
                or json.loads(bound['receipt_json']) != receipt):
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    return {'version': VERSION, 'kind': 'current_execution_receipt', 'role': 'tool',
        'content': bound['receipt_json'],
        'provenance': {'scope': 'isolated_local_execution_fixture', 'window': window, 'turn': turn,
            'tool_call_id': bound['tool_call_id'], 'status': bound['status'],
            'execution_sha256': bound['execution_sha256'], 'receipt_sha256': bound['receipt_sha256']},
        'authority': 'Verified local fixture execution result for this exact isolated window/turn/tool call. Error or unavailable is not success; absent results are unknown. This does not establish production host authentication.'}
