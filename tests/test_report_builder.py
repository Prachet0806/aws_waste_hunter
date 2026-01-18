from reporting.report_builder import build_report


def test_report_includes_empty_sections_and_errors():
    report = build_report([], 0, [], scan_errors=[{"stage": "scan_x", "error": "oops"}], delivery_errors=[])

    assert "## Wasted Resources" in report
    assert "- None detected" in report
    assert "## Tagging Violations" in report
    assert "## Scan/Delivery Errors" in report
    assert "scan_x" in report
