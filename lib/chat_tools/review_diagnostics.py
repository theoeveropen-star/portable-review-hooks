"""Finite diagnostic vocabulary; never includes provider/model output text."""
import math
import re
CONTRACT_ERRORS = frozenset(['anchor_not_in_complete_draft', 'assistant_claim_cannot_prove_conflict', 'current_issue_must_not_query_history', 'current_issue_requires_evidence', 'duplicate_review_issue', 'history_request_requires_user_evidence', 'invalid_complete_draft', 'invalid_current_evidence_ids', 'invalid_current_user', 'invalid_draft_anchor', 'invalid_draft_provenance', 'invalid_input_coverage', 'invalid_issue_kind', 'invalid_issue_query', 'invalid_recall_scope', 'invalid_recent_context', 'invalid_recent_message', 'invalid_review_input', 'invalid_review_issue', 'invalid_review_json', 'invalid_review_plan', 'invalid_short_plan', 'invalid_span_plan', 'route_issue_mismatch'])
FINISH_REASONS = frozenset(('stop', 'length', 'content_filter', 'tool_calls', 'function_call', 'end_turn', 'max_tokens', 'stop_sequence'))
HASH_FIELDS = ('request_sha256', 'response_sha256', 'input_sha256', 'prompt_sha256', 'model_output_sha256', 'projection_sha256')
COUNT_FIELDS = ('request_bytes', 'response_bytes', 'model_output_bytes', 'max_output_tokens', 'projection_bytes', 'draft_span_count', 'response_bytes_received')

TIMING_FIELDS = ('request_started_ms', 'headers_received_ms', 'first_body_byte_ms', 'full_body_ms', 'validated_ms')

def safe_plan_diagnostics(raw):
    raw = raw if isinstance(raw, dict) else {}
    metadata = raw.get('metadata') if isinstance(raw.get('metadata'), dict) else {}
    out = {}
    code = raw.get('contract_error', metadata.get('contract_error'))
    if isinstance(code, str) and code in CONTRACT_ERRORS:
        out['contract_error'] = code
    finish = metadata.get('finish_reason', metadata.get('stop_reason'))
    if isinstance(finish, str) and finish in FINISH_REASONS:
        out['finish_reason'] = finish
    for key in HASH_FIELDS:
        if isinstance(metadata.get(key), str) and re.fullmatch('[a-f0-9]{64}', metadata[key]):
            out[key] = metadata[key]
    for key in COUNT_FIELDS:
        if type(metadata.get(key)) is int and 0 <= metadata[key] <= 2147483647:
            out[key] = metadata[key]
    for key in TIMING_FIELDS:
        value = metadata.get(key)
        if type(value) in (int, float) and 0 <= value <= 1.7976931348623157e308 and math.isfinite(value):
            out[key] = value
    if metadata.get('short_plan_version') in ('companion-r3-short-plan/v1', 'companion-r3-span-plan/v1'):
        out['short_plan_version'] = metadata['short_plan_version']
    return out
