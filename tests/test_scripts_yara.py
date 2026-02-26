from xdr_cli.services.scripts import build_py_script_from_yara_bytes


def test_build_py_script_from_yara_injects_content():
    script = build_py_script_from_yara_bytes(b'rule test { condition: true }')
    assert 'XXX_XDR_CLI_INSERT_YARA_CONTENT_XXX' not in script
    assert 'base64.b64decode' in script
