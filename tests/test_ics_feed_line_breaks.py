"""Sign-up/settings launch audit 11/13 - the calendar feed (.ics) never carries a raw carriage return.
A task note typed on Windows or pasted from an email has \\r\\n line breaks; a bare \\r inside an .ics
line breaks some calendar apps' import."""
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

from app.services.tasks_service import _ics_escape  # noqa: E402


def test_windows_and_mac_line_breaks_become_ics_newlines():
    assert _ics_escape("Call Pat\r\nabout 12 Elm; bring keys, flyers") == "Call Pat\\nabout 12 Elm\\; bring keys\\, flyers"
    assert _ics_escape("one\rtwo\nthree") == "one\\ntwo\\nthree"
    assert "\r" not in _ics_escape("a\r\r\nb\r")


def test_plain_text_unchanged_and_empty_safe():
    assert _ics_escape("Open house at 3") == "Open house at 3"
    assert _ics_escape("") == "" and _ics_escape(None) == ""
    assert _ics_escape("C:\\path") == "C:\\\\path"
