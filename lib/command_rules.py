# -*- coding: utf-8 -*-
"""
Auto-accept rules for Cursor tool confirmations.

Checks command text against configurable allow/deny patterns.
Deny always overrides allow. Config lives in lib/command_rules.json
and is hot-reloaded on change.
"""
import json
import re
import fnmatch
from pathlib import Path

_RULES_FILE = Path(__file__).parent / 'command_rules.json'
_rules_mtime = 0.0
_rules = {'allow': [], 'deny': []}

# Button priority: most conservative first. "Allow" grants broader
# permission (whole directory) -- deliberately excluded for now.
_ACCEPT_KEYWORDS = ('accept', 'run', 'fetch')


def _flatten_patterns(entries):
    """Flatten grouped or flat pattern lists into a single lowercase list."""
    result = []
    for entry in entries:
        if isinstance(entry, str):
            result.append(entry.lower())
        elif isinstance(entry, dict):
            for p in entry.get('patterns', []):
                result.append(p.lower())
    return result


def _reload_if_changed():
    global _rules, _rules_mtime
    if not _RULES_FILE.exists():
        return
    try:
        mt = _RULES_FILE.stat().st_mtime
        if mt == _rules_mtime:
            return
        data = json.loads(_RULES_FILE.read_text(encoding='utf-8'))
        _rules = {
            'allow': _flatten_patterns(data.get('allow', [])),
            'deny': _flatten_patterns(data.get('deny', [])),
        }
        _rules_mtime = mt
        print(f"[command-rules] Loaded {len(_rules['allow'])} allow, "
              f"{len(_rules['deny'])} deny patterns")
    except Exception as e:
        print(f"[command-rules] Failed to load rules: {e}")


def match(command_text):
    """Check command text against allow/deny rules.

    Deny scans the FULL text (catches dangerous keywords anywhere).
    Chained commands (;, &&, ||) are split — ALL parts must match allow.

    Returns 'accept', 'deny', or None (ask user).
    """
    _reload_if_changed()
    if not _rules['allow']:
        return None

    full_text = command_text.lower().strip()
    if not full_text:
        return None

    if any(kw in full_text for kw in _rules['deny']):
        return 'deny'

    # Extract actual command (DOM shows "Run command: ls $ ls")
    cmd = full_text.split('$ ', 1)[1].strip() if '$ ' in full_text else full_text

    parts = [p.strip() for p in re.split(r'\s*(?:;|&&|\|\|)\s*', cmd) if p.strip()]
    if not parts:
        return None

    def allowed(part):
        return any(fnmatch.fnmatch(part, pat) or fnmatch.fnmatch(part, pat.rstrip(' *'))
                   for pat in _rules['allow'])

    if all(allowed(p) for p in parts):
        return 'accept'
    return None


def find_accept_button(buttons):
    """Find the most conservative accept button.

    Priority: accept > run > fetch.
    "Allow" is deliberately excluded (grants broader directory permission).
    """
    for keyword in _ACCEPT_KEYWORDS:
        for btn in buttons:
            if keyword in btn['label'].lower():
                return btn['index'], btn['label']
    return None, None
