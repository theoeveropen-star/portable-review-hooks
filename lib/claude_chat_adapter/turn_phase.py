"""Content-free progress of one live turn, for the App's companion status (display only).

A phase rides the existing companion.activity channel as a pseudo tool named
``companion.phase.<phase>`` (ASCII, so the bridge's activity contract accepts it
unchanged). It never carries draft text, tool input or review material, and
it never decides anything: losing a phase frame changes nothing but the label.

Phases: prepare (before the provider is called), thinking, writing,
review (stop-hook review of a finished draft). Tools keep their own activity.
"""
import json
import threading

PHASES = ('prepare', 'thinking', 'writing', 'review')
PREFIX = 'companion.phase.'
# The bridge accepts at most 128 activity ids per stream and the turn may
# call up to 80 tools; phases stop well below that so a real tool always fits.
LIMIT = 32


def _lock(running):
    lock = running.get('_phase_lock')
    if lock is None:
        lock = running.setdefault('_phase_lock', threading.Lock())
    return lock


def set_phase(notify, running, phase):
    """Close the open phase and open ``phase`` (None closes only). Never raises.

    ``'tool'`` closes the open phase without opening one: the tool's own
    activity frame is the label, and queue_status reports ``tool``.
    """
    try:
        if running is None or (phase is not None and phase != 'tool' and phase not in PHASES):
            return
        with _lock(running):
            running['_phase_tool'] = phase == 'tool'
            if phase == 'tool':
                phase = None
            current = running.get('_phase')
            if current is not None and current[1] == phase:
                return
            if current is not None:
                notify(running, 'activity', {'id': current[0], 'kind': 'tool',
                                             'name': PREFIX + current[1], 'status': 'completed'})
                running['_phase'] = None
            if phase is None:
                return
            count = running.get('_phase_count', 0)
            if count >= LIMIT:
                return
            running['_phase_count'] = count + 1
            identifier = 'companion-phase-%d' % (count + 1)
            running['_phase'] = (identifier, phase)
            notify(running, 'activity', {'id': identifier, 'kind': 'tool',
                                         'name': PREFIX + phase, 'status': 'running'})
            # Metadata only (no text): which phases a turn really reached.
            print(json.dumps({'event': 'companion_turn_phase', 'phase': phase, 'seq': count + 1,
                              'client_sha256': running.get('_client_sha256')}), flush=True)
    except Exception:
        pass


def current(running):
    """The open phase name, or None. Read-only (queue_status)."""
    try:
        if running is None:
            return None
        value = running.get('_phase')
        return value[1] if value else 'tool' if running.get('_phase_tool') else None
    except Exception:
        return None


def from_stream_event(event):
    """Phase implied by a main-loop CLI stream event, or None."""
    if not isinstance(event, dict) or event.get('type') != 'stream_event' or event.get('parent_tool_use_id'):
        return None
    current = event.get('event')
    if not isinstance(current, dict) or current.get('type') != 'content_block_start':
        return None
    block = current.get('content_block')
    kind = block.get('type') if isinstance(block, dict) else None
    if kind in ('thinking', 'redacted_thinking'):
        return 'thinking'
    if kind == 'text':
        return 'writing'
    if kind in ('tool_use', 'server_tool_use'):
        return 'tool'  # A tool's own activity takes over; close the phase.
    return None
