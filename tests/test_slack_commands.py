from xdr_cli.slack.commands import parse_slack_command


def test_parse_slack_command_extracts_tenant_and_options():
    cmd = parse_slack_command('endpoint get --tenant demo --field endpoint_id host1')
    assert cmd.group == 'endpoint'
    assert cmd.action == 'get'
    assert cmd.tenant == 'demo'
    assert cmd.options['field'] == 'endpoint_id'
    assert cmd.positional == ['host1']
