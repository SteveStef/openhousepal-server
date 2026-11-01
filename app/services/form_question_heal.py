"""Form questions that arrived as JSON text instead of a list (11/01: a form made from the AI helper
chat showed '[{"label": "Do you have a kitchen ...", "type": "select", ...' as its question - the
model sent the list as text that didn't parse, so the whole text became one label, cut at 200
characters). parse_blob recovers the questions from such text, complete or cut off; heal_questions
repairs a saved form's list (used when forms load, so a form saved that way fixes itself)."""
from __future__ import annotations

import json
import re
from typing import Optional

_STR = r'"((?:[^"\\]|\\.)*)"'
_OBJ = re.compile(r'\{\s*"label"\s*:\s*' + _STR + r'([^{}]*)', re.S)
_TYPE = re.compile(r'"type"\s*:\s*"(\w+)"')
_OPTS = re.compile(r'"options"\s*:\s*\[([^\]]*)')
_REQ = re.compile(r'"required"\s*:\s*(t|true|True)\b')
CHOICE = ("select", "multi_select")


def looks_like_blob(text) -> bool:
    t = str(text or "").lstrip()
    return t.startswith("[{") or t.startswith('{"') or t.startswith("[\"")


def _unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s


def parse_blob(text: str) -> Optional[list]:
    """The questions in JSON-ish text: a real parse when it's whole, else each {"label": ...} piece
    that survived (a cut-off last one keeps whatever made it - a pick-one with under two answers
    becomes a short answer). None when nothing usable is in it."""
    t = str(text or "").strip()
    try:
        data = json.loads(t)
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list):
            out = [d if isinstance(d, dict) else {"label": str(d)} for d in data if d]
            return out or None
    except ValueError:
        pass
    out = []
    for m in _OBJ.finditer(t):
        label, rest = _unescape(m.group(1)).strip(), m.group(2)
        if not label:
            continue
        q = {"label": label}
        if (tm := _TYPE.search(rest)):
            q["type"] = tm.group(1)
        if (om := _OPTS.search(rest)):
            # only answers whose closing quote arrived
            q["options"] = [_unescape(o).strip() for o in re.findall(_STR, om.group(1)) if o.strip()]
        if _REQ.search(rest):
            q["required"] = True
        if q.get("type") in CHOICE and len(q.get("options") or []) < 2:
            q["type"] = "short_text"
            q.pop("options", None)
        out.append(q)
    return out or None


def heal_questions(questions) -> Optional[list]:
    """A saved form's questions with any JSON-text question opened back up into real ones (same
    id for the first, -2, -3 ... after). None when nothing needed it."""
    if not isinstance(questions, list) or not any(isinstance(q, dict) and looks_like_blob(q.get("label")) for q in questions):
        return None
    out = []
    for q in questions:
        if not (isinstance(q, dict) and looks_like_blob(q.get("label"))):
            out.append(q)
            continue
        found = parse_blob(q["label"]) or []
        for i, f in enumerate(found):
            qt = f.get("type") if f.get("type") in ("short_text", "long_text", "select", "multi_select", "number", "currency", "date", "time", "email", "phone") else "short_text"
            opts = [str(o)[:80] for o in (f.get("options") or [])][:20]
            if qt in CHOICE and len(opts) < 2:
                qt, opts = "short_text", []
            out.append({**{k: v for k, v in q.items() if k not in ("label", "type", "options", "required")},
                        "id": q.get("id") if i == 0 else f"{q.get('id') or 'q'}-{i + 1}",
                        "label": str(f.get("label") or "")[:200], "type": qt, "required": bool(f.get("required")),
                        **({"options": opts} if qt in CHOICE else {})})
    return out
