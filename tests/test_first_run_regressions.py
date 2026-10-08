"""First-run contracts: offline, no credentials, no live installation writes."""
from __future__ import annotations
from wsp_core import config
from wsp_core import config as config_module
from wsp_core import extract

import json
from pathlib import Path
import sys

import pytest

from wsp_core import search

from plugin_loader import load_plugin
ROOT = Path(__file__).resolve().parents[1]
wsp = load_plugin("wsp_first_run_test")


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    for item in wsp._get_provider_catalog():
        monkeypatch.delenv(item['env'], raising=False)
    monkeypatch.delenv('KEENABLE_ALLOW_PUBLIC', raising=False)


def test_empty_dashboard_recommends_setup_not_search():
    text = wsp._render_status_dashboard(wsp._provider_config_status({}), color=False)
    assert 'python3 ~/.hermes/plugins/web-search-plus/setup.py setup --preset starter' in text
    assert 'search.py --query' not in text


def test_configured_dashboard_recommends_search_and_reload():
    status = wsp._provider_config_status({'SERPER_API_KEY': 'fake-test-key'})
    text = wsp._render_status_dashboard(status, color=False)
    assert 'python3 ~/.hermes/plugins/web-search-plus/search.py --query' in text
    assert '/reset' in text
    assert 'restart' not in text.lower()  # No unsafe generic restart prescription.


def test_starter_badges_match_preset():
    expected = {item['provider'] for item in wsp._providers_for_preset('starter')}
    actual = {item['provider'] for item in wsp._get_provider_catalog() if item['recommended']}
    assert actual == expected == {'you', 'serper', 'linkup'}


def test_standard_profile_is_not_ready_with_no_provider():
    profile = wsp._status_payload(env={}, config={'profile': 'standard'})['profile']
    assert profile['ready'] is False
    assert profile['policy_ready'] is True


@pytest.mark.parametrize('env,provider_config', [
    ({'SERPER_API_KEY': 'fake-test-key'}, {}),
    ({}, {'serper': {'api_key': 'fake-test-key'}}),
    ({}, {'keenable': {'allow_public': True}}),
])
def test_standard_profile_ready_for_supported_configuration(env, provider_config):
    payload = wsp._status_payload(env=env, config={'profile': 'standard', **provider_config})
    assert payload['profile']['ready'] is True
    assert payload['providers']['search_configured'] is True


def test_invalid_self_hosted_profile_not_ready_with_cloud_key():
    payload = wsp._status_payload(env={'SERPER_API_KEY': 'fake-test-key'}, config={'profile': 'self_hosted'})
    assert payload['profile']['ready'] is False
    assert payload['profile']['policy_ready'] is False


def test_missing_key_guidance_uses_setup_and_env_not_inline_config():
    with pytest.raises(config.ProviderConfigError) as exc:
        config.validate_api_key('serper', {})
    payload = json.loads(str(exc.value))
    guidance = '\n'.join(payload['how_to_fix'])
    assert 'setup.py setup serper' in guidance
    assert '.env' in guidance
    assert 'config.json' not in guidance
    assert payload['env_var'] == 'SERPER_API_KEY'


def test_no_key_search_has_top_level_actionable_error():
    args = search.build_parser(config_module._deepcopy_default_config()).parse_args(['--query', 'offline first run', '--no-cache'])
    payload, code = search.execute_search_request(args, config_module._deepcopy_default_config())
    assert code != 0
    assert payload['error_type'] == 'provider_setup_required'
    assert 'setup.py setup --preset starter' in '\n'.join(payload['how_to_fix'])
    assert isinstance(payload['provider_errors'][0]['error'], str)
    assert not payload['provider_errors'][0]['error'].startswith('{')


def test_native_search_no_key_has_actionable_error():
    payload = search.run_search_request(query='offline first run', no_cache=True, config=config_module._deepcopy_default_config())
    assert payload['error_type'] == 'provider_setup_required'
    assert 'setup.py setup --preset starter' in '\n'.join(payload['how_to_fix'])
    assert not payload['provider_errors'][0]['error'].startswith('{')


def test_human_status_matches_config_only_provider():
    text = wsp._render_setup_guidance(env={}, fancy=True, config={'keenable': {'allow_public': True}})
    assert 'search.py --query' in text
    assert 'none yet' not in text


def test_setup_guidance_does_not_relabel_configured_provider_failure():
    payload = {'error': 'upstream network failed'}
    config.add_provider_setup_guidance(payload, 'search', ['serper'], {'serper': {'api_key': 'fake-test-key'}})
    assert payload == {'error': 'upstream network failed'}


def test_setup_guidance_respects_keyless_opt_in():
    payload = {'error': 'upstream network failed'}
    config.add_provider_setup_guidance(payload, 'search', ['keenable'], {'keenable': {'allow_public': True}})
    assert payload == {'error': 'upstream network failed'}


def test_no_key_extract_has_actionable_error():
    payload = extract.extract_plus(['https://example.com'], config=config_module._deepcopy_default_config())
    assert payload['error_type'] == 'provider_setup_required'
    assert 'setup.py setup --preset extract' in '\n'.join(payload['how_to_fix'])
    assert payload['env_vars']


def _preset_env_vars(preset):
    return sorted(item['env'] for item in wsp._providers_for_preset(preset))


@pytest.mark.parametrize('capability,preset,profile', [
    ('search', 'starter', 'standard'),
    ('extract', 'extract', 'standard'),
    ('search', 'self-hosted', 'self_hosted'),
])
def test_auto_guidance_env_vars_match_recommended_preset(capability, preset, profile):
    payload = {'error': 'All providers failed'}
    cfg = {**config_module._deepcopy_default_config(), 'profile': profile}
    candidates = ['searxng'] if profile == 'self_hosted' else ['tavily']
    config.add_provider_setup_guidance(payload, capability, candidates, cfg)
    assert f'--preset {preset}' in payload['error']
    assert sorted(payload['env_vars']) == _preset_env_vars(preset)


def test_cli_no_key_extract_env_vars_match_extract_preset():
    payload = extract.extract_plus(['https://example.com'], config=config_module._deepcopy_default_config())
    assert sorted(payload['env_vars']) == _preset_env_vars('extract')


def test_cli_no_key_search_env_vars_match_starter_preset():
    args = search.build_parser(config_module._deepcopy_default_config()).parse_args(['--query', 'offline first run', '--no-cache'])
    payload, _ = search.execute_search_request(args, config_module._deepcopy_default_config())
    assert sorted(payload['env_vars']) == _preset_env_vars('starter')


@pytest.mark.parametrize('payload,expected_code', [
    ({'error': 'failed', 'results': []}, 1),
    ({'results': [{'url': 'https://example.com', 'error': 'failed'}]}, 1),
    ({'results': [{'url': 'https://example.com', 'content': 'ok'}]}, 0),
    ({'results': [{'url': 'https://example.com', 'error': 'failed'}, {'url': 'https://example.org', 'content': 'ok'}]}, 0),
])
def test_extract_cli_exit_matches_result(monkeypatch, capsys, payload, expected_code):
    monkeypatch.setattr(sys, 'argv', ['search.py', '--extract-urls', 'https://example.com'])
    monkeypatch.setattr(search, 'load_config', config_module._deepcopy_default_config)
    monkeypatch.setattr(search, 'extract_plus', lambda **kw: payload)
    if expected_code:
        with pytest.raises(SystemExit) as exc:
            search.main()
        assert exc.value.code == expected_code
    else:
        search.main()
    streams = capsys.readouterr()
    assert json.loads(streams.err if expected_code else streams.out) == payload


@pytest.mark.parametrize('entrypoint', ['cli-core', 'native'])
def test_explicit_missing_key_targets_requested_provider(monkeypatch, entrypoint):
    monkeypatch.setenv('YOU_API_KEY', 'fake-local-test-key')
    cfg = config_module._deepcopy_default_config()
    if entrypoint == 'cli-core':
        args = search.build_parser(cfg).parse_args(['--query', 'offline explicit', '--provider', 'serpbase', '--no-cache'])
        payload, code = search.execute_search_request(args, cfg)
        assert code != 0
    else:
        payload = search.run_search_request(query='offline explicit', provider='serpbase', no_cache=True, config=cfg)
    assert payload['error_type'] == 'requested_provider_not_configured'
    assert "Requested provider 'serpbase' is not configured" in payload['error']
    assert 'setup.py setup serpbase' in '\n'.join(payload['how_to_fix'])
    assert '--preset starter' not in '\n'.join(payload['how_to_fix'])
    assert payload['env_vars'] == ['SERPBASE_API_KEY']


def test_explicit_extract_missing_key_targets_requested_provider():
    payload = extract.extract_plus(['https://example.com'], provider='firecrawl', config=config_module._deepcopy_default_config())
    assert payload['error_type'] == 'requested_provider_not_configured'
    assert 'setup.py setup firecrawl' in '\n'.join(payload['how_to_fix'])
    assert payload['env_vars'] == ['FIRECRAWL_API_KEY']


def test_fastpath_missing_config_is_not_inspected(tmp_path):
    payload = wsp._build_fastpath_report(tmp_path / 'missing.yaml')
    check = next(item for item in payload['checks'] if item['id'] == 'hermes_config_found')
    assert check['ok'] is False
    assert 'missing' in check['detail'].lower()
    assert 'inspected' not in check['detail'].lower()
