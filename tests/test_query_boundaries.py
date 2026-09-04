"""Public search requests must remain data, never command-line options."""
import pytest

import extract
import search
from config import _deepcopy_default_config
from contract_v3 import RequestV3


@pytest.fixture
def runtime_config(tmp_path, monkeypatch):
    config = _deepcopy_default_config()
    config['serper']['api_key'] = 'test-serper-key'
    config.setdefault('v3', {})
    config['v3']['state_path'] = str(tmp_path / 'state.sqlite3')
    config['v3']['cache_dir'] = str(tmp_path / 'cache')
    config['quality']['filter_spam'] = False
    monkeypatch.setattr(search, 'load_config', lambda: config)
    return config


@pytest.mark.parametrize('query', ['--help', '-site:reddit.com', '--', '-q=other', 'normal query', '日本語 query'])
def test_public_search_preserves_query(query, runtime_config, monkeypatch):
    seen = []

    def provider(**kwargs):
        seen.append(kwargs['query'])
        return {
            'provider': 'serper', 'query': kwargs['query'],
            'results': [{'title': 'Fixture source', 'url': 'https://example.org/source', 'snippet': 'Source text'}],
            'images': [], 'answer': '', 'metadata': {},
        }

    monkeypatch.setattr(search, 'search_serper', provider)
    result = search.run_search_request(query=query, provider='serper', count=1)
    assert seen == [query]
    assert result['query'] == query
    assert result['results'][0]['url'] == 'https://example.org/source'


def test_v3_argument_projection_preserves_controls(runtime_config):
    request = RequestV3.from_dict({
        'contract_version': '3.0',
        'capability': 'search', 'input': {'query': '--help'},
        'routing': {'provider': 'serper', 'mode': 'fixed', 'allow_fallback': True},
        'options': {
            'max_results': 7, 'depth': 'normal', 'time_range': 'week',
            'freshness': 'day', 'search_type': 'news',
            'include_domains': ['example.org'], 'exclude_domains': ['example.net'],
            'mode': 'research', 'research_time_budget': 12, 'quality_report': True,
            'locale': {'country': 'at', 'language': 'de'},
        },
        'cache': {'mode': 'bypass', 'ttl_seconds': 120},
    })
    args = search._search_args_from_v3(request, runtime_config)
    assert (args.query, args.provider, args.max_results) == ('--help', 'serper', 7)
    assert (args.time_range, args.freshness, args.search_type) == ('week', 'day', 'news')
    assert args.include_domains == ['example.org']
    assert args.exclude_domains == ['example.net']
    assert (args.country, args.language) == ('at', 'de')
    assert (args.mode, args.research_time_budget, args.quality_report) == ('research', 12, True)
    assert args.allow_fallback is True
    assert args.no_cache is True
    assert args.cache_ttl == 120


@pytest.mark.parametrize('module', [search, extract], ids=['search', 'extract'])
@pytest.mark.parametrize('section', ['bad-shape', ['bad-shape'], True])
def test_invalid_budget_section_is_a_config_error(module, section, monkeypatch):
    monkeypatch.delenv('WSP_BUDGET_PREFLIGHT_OFF', raising=False)
    with pytest.raises(ValueError, match='budget_preflight must be an object'):
        module._daily_preflight_budget({'budget_preflight': section})
