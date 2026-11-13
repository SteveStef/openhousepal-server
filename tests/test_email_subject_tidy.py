"""Email subjects: MLS capitals in an address get tidied, times keep AM / PM (audit 11/13: a client's
"Your tour ... is confirmed - Saturday, November 14 at 12:00 PM" arrived as "12:00 Pm")."""
from app.services.email_service import _tidy_subject


def test_time_keeps_pm():
    s = "Your tour of 181 Hunt Valley Cir is confirmed - Saturday, November 14 at 12:00 PM"
    assert _tidy_subject(s) == s
    assert _tidy_subject("Showing at 2 PM today") == "Showing at 2 PM today"
    assert _tidy_subject("Open house 1:00 PM - 3:00 PM ET") == "Open house 1:00 PM - 3:00 PM ET"


def test_caps_address_still_tidied():
    assert _tidy_subject("Jane wants to host 1650 SKINNERS TURN RD, WAYNE, PA 19087") == \
        "Jane wants to host 1650 Skinners Turn Rd, Wayne, PA 19087"
    assert _tidy_subject("New Tour Request - 2 MARTINS LANE") == "New Tour Request - 2 Martins Lane"
    assert _tidy_subject("Tour of 731 S 21ST ST at 4:30 PM") == "Tour of 731 S 21st St at 4:30 PM"


def test_empty_unit_dropped():
    assert _tidy_subject("New comment - 1330 N Avignon Dr #, Gladwyne") == "New comment - 1330 N Avignon Dr, Gladwyne"
