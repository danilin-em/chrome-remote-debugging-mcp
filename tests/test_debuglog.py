"""The debug logger is off unless MCP_DEBUG_LOG turns it on."""

import pytest

import chrome_remote_debugging_mcp.debuglog as debuglog


@pytest.mark.parametrize("raw", [None, "", "  ", "0", "false", "No", "OFF"])
def test_log_path_is_off_by_default_and_for_falsy_values(raw):
    assert debuglog._log_path(raw) is None


@pytest.mark.parametrize("raw", ["1", "true", "Yes", "on"])
def test_log_path_uses_the_default_file_for_truthy_values(raw):
    assert debuglog._log_path(raw) == debuglog.DEFAULT_PATH


def test_log_path_takes_any_other_value_as_the_file_path():
    assert debuglog._log_path(" /var/log/mcp.log ") == "/var/log/mcp.log"
