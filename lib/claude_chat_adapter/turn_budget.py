"""Shared, response-bound capacity accounting for one live native turn.

This is a conservative admission bound, not a provider token receipt. Copies of
the request row retain this object. Every MCP result, including local archive
pages, is admitted under the same lock; siblings cannot each spend the same room.
"""
import base64
import binascii
import json
import math
import struct
import threading

from .slim_transcript import estimate

# 2026-10-28: slim_transcript.estimate counts every image as 16000 tokens. Five
# 350x512 paintbrush thumbnails (~240 real tokens each) then read as 80k and
# asked for a needless note + rebuild at 12:09. For this trigger only, an image
# whose size can be read costs its provider pixel tokens (w*h/750 after the
# long edge is fitted to IMAGE_LONG_EDGE) plus framing; otherwise still 16000.
IMAGE_LONG_EDGE = 2576
IMAGE_FRAMING = 64
IMAGE_UNKNOWN = 16000
_HEAD_CHARS = 349528  # base64 of the first 256 KiB; enough for any header we read


def image_size(raw):
    """(width, height) from PNG/GIF/WebP/JPEG header bytes, or None."""
    try:
        if raw[:8] == b'\x89PNG\r\n\x1a\n' and raw[12:16] == b'IHDR':
            return struct.unpack('>II', raw[16:24])
        if raw[:6] in (b'GIF87a', b'GIF89a'):
            return struct.unpack('<HH', raw[6:10])
        if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
            kind = raw[12:16]
            if kind == b'VP8X':
                return (1 + int.from_bytes(raw[24:27], 'little'), 1 + int.from_bytes(raw[27:30], 'little'))
            if kind == b'VP8L' and raw[20] == 0x2f:
                bits = int.from_bytes(raw[21:25], 'little')
                return ((bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1)
            if kind == b'VP8 ' and raw[23:26] == b'\x9d\x01\x2a':
                w, h = struct.unpack('<HH', raw[26:30])
                return (w & 0x3fff, h & 0x3fff)
            return None
        if raw[:2] == b'\xff\xd8':
            i = 2
            while i + 9 < len(raw):
                if raw[i] != 0xff:
                    return None
                marker = raw[i + 1]
                if marker in (0xd8, 0x01) or 0xd0 <= marker <= 0xd7:
                    i += 2
                    continue
                length = struct.unpack('>H', raw[i + 2:i + 4])[0]
                if 0xc0 <= marker <= 0xcf and marker not in (0xc4, 0xc8, 0xcc):
                    h, w = struct.unpack('>HH', raw[i + 5:i + 9])
                    return (w, h)
                i += 2 + length
    except (IndexError, struct.error, TypeError):
        return None
    return None


def image_tokens(block):
    """Provider pixel tokens of one MCP/Anthropic image block, or None when unreadable."""
    data = block.get('data')
    if not isinstance(data, str):
        source = block.get('source')
        data = source.get('data') if isinstance(source, dict) else None
    if not isinstance(data, str) or not data:
        return None
    head = data[:_HEAD_CHARS]
    try:
        raw = base64.b64decode(head[:len(head) - len(head) % 4], validate=False)
    except (binascii.Error, ValueError):
        return None
    size = image_size(raw)
    if not size or size[0] <= 0 or size[1] <= 0:
        return None
    w, h = size
    scale = min(1.0, IMAGE_LONG_EDGE / max(w, h))
    w, h = max(1, round(w * scale)), max(1, round(h * scale))
    return math.ceil(w * h / 750) + IMAGE_FRAMING


def result_estimate(value):
    """slim_transcript.estimate, except readable images cost their pixel tokens."""
    if isinstance(value, dict) and value.get('type') == 'image':
        tokens = image_tokens(value)
        return IMAGE_UNKNOWN if tokens is None else tokens
    if isinstance(value, dict):
        return sum(estimate(k) + result_estimate(v) for k, v in value.items())
    if isinstance(value, list):
        return sum(result_estimate(v) for v in value)
    return estimate(value)


def wire_bound(result):
    # Include both content and structuredContent, Unicode/control escaping, the
    # CLI's possible JSON-as-text wrapping, and the tool_result protocol envelope.
    encoded = json.dumps(result, ensure_ascii=True, separators=(',', ':'))
    return len(json.dumps(encoded, ensure_ascii=True)) + 128


def finish_result(*, executed=False, archive_ref=None):
    text = ('context_capacity_finish_required; ' +
            ('tool returned; do not repeat it. ' if executed else 'not_executed. ') +
            'Finish the reply now; continue reads on a later user turn.')
    if archive_ref:
        text += ' Saved result: context_slim read_tool_result archive_ref=' + archive_ref
    return {'isError': not executed, 'content': [{'type': 'text', 'text': text}]}


class TurnBudget:
    def __init__(self, initial_tokens, output_reserve=12000, max_calls=32):
        self.lock = threading.RLock()
        self.observed = max(0, initial_tokens or 0)
        self.message_id = None
        self.seen = set()
        self.reserved = 0
        # Token estimate of the same delivered results (slim_transcript.estimate:
        # ASCII ~3 chars/token, other Unicode 1.5 tokens/codepoint, +15%; readable
        # images by pixels, see result_estimate). Only the authored-note trigger
        # reads it; admission keeps the wire bound.
        self.reserved_tokens = 0
        # Keep the configured output allowance plus enough small refusal frames
        # for a full admitted tool batch. No provider limit is increased.
        self.finish_reserve = output_reserve + max(32768, max_calls * 512)
        self.output_reserve = output_reserve
        self.review_sealed = False
        # One bounded recovery allowance for this live turn, never renewed by
        # another provider usage snapshot. All costs also enter reserved below.
        self.recovery_spent = 0

    def seal_for_review(self):
        """Release unused future-tool room only after the host durably seals tools.

        The host must first persist its seal and drain already-forwarded calls.
        Already observed input and charged results remain reserved; the output
        allowance and a small refusal allowance stay inside the same ceiling.
        """
        with self.lock:
            self.finish_reserve = min(self.finish_reserve, self.output_reserve + 8000)
            self.review_sealed = True

    def observe(self, tokens, message_id):
        if type(tokens) is not int or tokens < 0 or not isinstance(message_id, str) or not message_id:
            return
        with self.lock:
            if message_id not in self.seen:
                self.seen.add(message_id)
                self.message_id = message_id
                # The new provider request has consumed prior tool responses.
                # A duplicate/late snapshot never releases newer reservations.
                self.reserved = 0
                self.reserved_tokens = 0
            self.observed = max(self.observed, tokens)

    def slim_threshold(self, ceiling, configured):
        # Use the same conservative units as tool admission. Leave room for a
        # result and a note offer BEFORE the ordinary admission gate closes.
        return max(0, min(configured, ceiling - self.finish_reserve - 16384))

    def slim_estimate(self):
        with self.lock:
            return self.observed + self.reserved_tokens

    def slim_due(self, ceiling, configured):
        # 2026-10-26: the double-escaped wire bound counted 42.5k real tokens as
        # 120k and asked for a note mid-turn. Threshold and admission unchanged.
        with self.lock:
            return self.slim_estimate() >= self.slim_threshold(ceiling, configured)

    def available(self, ceiling, *, recovery=False):
        with self.lock:
            normal = max(0, min(60000, ceiling - self.observed - self.reserved - self.finish_reserve))
            if recovery and not self.review_sealed:
                # Large max_calls reserves must not lock out the controls that
                # finish slimming. Use at most 24k total of actual hard headroom,
                # with output and 2k refusal room still protected. This is a
                # finite loan from existing reserve, not a higher model ceiling.
                hard_room = ceiling - self.observed - self.reserved - self.output_reserve - 2048
                return max(normal, max(0, min(24576 - self.recovery_spent, hard_room)))
            return normal

    def charge(self, result, *, recovery=False, ceiling=None):
        # Caller holds lock through choosing, constructing and charging a result.
        cost = wire_bound(result)
        if recovery:
            if ceiling is None:
                raise ValueError('recovery_ceiling_required')
            self.recovery_spent += max(0, cost - self.available(ceiling))
        self.reserved += cost
        self.reserved_tokens += result_estimate(result) + 64  # + tool_result framing
        return result

    def stop(self, *, executed=False, archive_ref=None):
        with self.lock:
            return self.charge(finish_result(executed=executed, archive_ref=archive_ref))


def page_result(report, text):
    page = {**report, 'text': text, 'next_offset': report['offset'] + len(text),
            'complete': report['offset'] + len(text) >= report['original_characters']}
    return {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(page, ensure_ascii=False)}],
            'structuredContent': page}


def bounded_page(result, budget):
    """Shrink a source-verified page without skipping or repeating its cursor."""
    report = result.get('structuredContent')
    if not isinstance(report, dict) or not isinstance(report.get('text'), str):
        return None
    if not all(type(report.get(k)) is int and report[k] >= 0 for k in ('offset', 'original_characters')):
        return None
    text = report['text']
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if wire_bound(page_result(report, text[:middle])) <= budget:
            low = middle
        else:
            high = middle - 1
    if not low and text:
        return None
    page = page_result(report, text[:low])
    return page if wire_bound(page) <= budget else None
