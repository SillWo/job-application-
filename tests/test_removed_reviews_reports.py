from backend.main import app


def test_removed_review_and_report_routes_are_absent_from_openapi() -> None:
    paths = app.openapi()["paths"]

    assert not any(path.startswith("/api/reviews") for path in paths)
    assert not any(path.startswith("/api/reports") for path in paths)
    assert "/api/sessions/{session_id}/report" in paths
    assert "/api/sessions/{session_id}/report/pdf" in paths
