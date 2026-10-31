"""Who the two people in the conversation are, read once from persona.local.json.

Names, pronoun and forms of address are installation data, never code. Prompts write {user_name},
{companion_name} and {user_pronoun}; parsers and recall heuristics take their word lists from here.

File: $COMPANION_PERSONA_FILE, else /etc/companion/persona.local.json. A missing file means the neutral defaults
below; a present but malformed file is an error (a typo must not silently rename the user).
"""
import json
import os
import re
from pathlib import Path

DEFAULTS = {
    'user_name': '用户',          # how prompts and story narration name the user
    'companion_name': '助手',     # how prompts and story narration name the companion
    'user_pronoun': 'TA',         # 她 / 他 / TA in model-facing notes
    'user_aliases': [],           # other words that mean the user (nicknames the companion uses)
    'companion_aliases': [],      # words the user calls the companion (a nickname, a family-style title …)
    'pet_names': [],              # affectionate address in either direction (terms of endearment …)
}
PATH = Path(os.environ.get('COMPANION_PERSONA_FILE', '/etc/companion/persona.local.json'))


def _load(path=PATH):
    data = dict(DEFAULTS)
    if not path.exists():
        return data
    raw = json.loads(path.read_text('utf-8'))
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError(f'persona_config_invalid_keys: {sorted(set(raw) - set(DEFAULTS)) if isinstance(raw, dict) else type(raw).__name__}')
    for key, value in raw.items():
        want = type(DEFAULTS[key])
        if not isinstance(value, want) or (want is list and not all(isinstance(v, str) and v.strip() for v in value)) \
                or (want is str and not value.strip()):
            raise ValueError(f'persona_config_invalid_value: {key}')
        data[key] = [v.strip() for v in value] if want is list else value.strip()
    return data


P = _load()
USER = P['user_name']
COMPANION = P['companion_name']
PRONOUN = P['user_pronoun']


def _uniq(*groups):
    out = []
    for group in groups:
        for word in ([group] if isinstance(group, str) else group):
            if word and word not in out:
                out.append(word)
    return tuple(out)


USER_WORDS = _uniq(USER, P['user_aliases'])                 # names that mean the user
COMPANION_WORDS = _uniq(COMPANION, P['companion_aliases'])  # names that mean the companion
PET_NAMES = _uniq(P['pet_names'])
VOCATIVES = _uniq(USER_WORDS, COMPANION_WORDS, PET_NAMES)   # any form of address at the start of a message


def alt(words):
    """Regex alternation of literal words, longest first; matches nothing when the list is empty."""
    words = sorted(set(words), key=len, reverse=True)
    return '|'.join(re.escape(w) for w in words) if words else '(?!)'


def fill(text):
    return (text.replace('{user_name}', USER).replace('{companion_name}', COMPANION)
            .replace('{user_pronoun}', PRONOUN))
