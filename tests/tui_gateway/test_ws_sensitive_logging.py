import pytest

from tui_gateway.ws import _parse_error_payload_preview


@pytest.mark.parametrize("method", ["secret.respond", "sudo.respond"])
def test_malformed_sensitive_response_payload_is_redacted(method):
    sentinel = "do-not-log-this-value"
    malformed = f'{{"method":"{method}","params":{{"value":"{sentinel}"'

    preview = _parse_error_payload_preview(malformed)

    assert preview == "<redacted sensitive response>"
    assert sentinel not in preview


def test_malformed_non_sensitive_payload_keeps_bounded_diagnostic_preview():
    malformed = '{"method":"session.list",' + ("x" * 500)

    preview = _parse_error_payload_preview(malformed)

    assert preview == malformed[:240]
