import pytest

from xdr_cli.errors import XdrValidationError
from xdr_cli.slack.commands import parse_slack_command


def test_parse_slack_command_extracts_tenant_and_options():
    cmd = parse_slack_command('endpoint get --tenant demo --field endpoint_id host1')
    assert cmd.group == 'endpoint'
    assert cmd.action == 'get'
    assert cmd.tenant == 'demo'
    assert cmd.options['field'] == 'endpoint_id'
    assert cmd.positional == ['host1']


def test_parse_slack_command_collects_flags():
    cmd = parse_slack_command('endpoint who-has 10.0.0.1 --raw')
    assert cmd.action == 'who-has'
    assert cmd.positional == ['10.0.0.1']
    assert 'raw' in cmd.flags
    assert cmd.options == {}


def test_parse_slack_command_requires_group_and_action():
    with pytest.raises(XdrValidationError):
        parse_slack_command('endpoint')


def test_parse_slack_command_missing_option_value():
    with pytest.raises(XdrValidationError):
        parse_slack_command('endpoint list --platform')


def test_parse_slack_command_quoted_query():
    cmd = parse_slack_command('xql run "dataset = xdr_data | fields x"')
    assert cmd.positional == ['dataset = xdr_data | fields x']
