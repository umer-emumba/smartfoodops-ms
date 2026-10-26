from app import sse


def test_event_is_one_json_data_line() -> None:
    assert sse.event("status", {"status": "confirmed", "note": "a\nb"}) == (
        'event: status\ndata: {"status":"confirmed","note":"a\\nb"}\n\n'
    )


def test_heartbeat_is_a_comment() -> None:
    assert sse.HEARTBEAT.startswith(":") and sse.HEARTBEAT.endswith("\n\n")
