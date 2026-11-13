"""Studio launch audit 11/13 - Bright sends an empty unit as a bare "#" ("601 ARGYLE CIR #"). It showed
on the client CMA page, the PDF and the CMA cards. A CMA address drops it; a real unit stays."""
from pydantic import TypeAdapter

from app.schemas.cma import CmaComparable, CmaCompCandidate, CmaReportListItem, CmaSubjectProperty, Street


def test_empty_unit_dropped_everywhere_a_cma_address_is_read():
    assert CmaSubjectProperty(street_address="601 ARGYLE CIR #").street_address == "601 ARGYLE CIR"
    assert CmaComparable(street_address="1169 Radnor Rd # ").street_address == "1169 Radnor Rd"
    assert TypeAdapter(Street).validate_python("601 ARGYLE CIR #, WYNNEWOOD, PA 19096") == "601 ARGYLE CIR, WYNNEWOOD, PA 19096"
    # The list card and the comp search use the same rule.
    for model in (CmaReportListItem, CmaCompCandidate):
        field = "subject_address" if model is CmaReportListItem else "street_address"
        assert any(getattr(m, "func", None).__name__ == "_drop_empty_unit" for m in model.model_fields[field].metadata)


def test_real_units_and_blanks_kept():
    assert CmaSubjectProperty(street_address="12 Main St #2B").street_address == "12 Main St #2B"
    assert CmaSubjectProperty(street_address="#5 Hall Ln").street_address == "#5 Hall Ln"
    assert CmaSubjectProperty(street_address=None).street_address is None
