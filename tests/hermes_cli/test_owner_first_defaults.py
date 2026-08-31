"""Behavioral contract for the owner-first fork defaults."""


def test_owner_first_defaults_execute_without_repeated_approval_prompts():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    approvals = DEFAULT_CONFIG["approvals"]
    assert approvals["mode"] == "off"
    assert approvals["cron_mode"] == "approve"
    assert approvals["single_query_mode"] == "approve"
    assert approvals["unattended_mode"] == "approve"
    assert approvals["mcp_reload_confirm"] is False
    assert approvals["destructive_slash_confirm"] is False
