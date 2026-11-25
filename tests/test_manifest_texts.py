"""What Hermes shows from plugin.yaml (`hermes plugins config`, onboarding) matches the engine.

Read as text like the other manifest tests: the test environment has no YAML library.
"""
import re
from pathlib import Path

from wsp_core.provider_registry import DEFAULT_AUTO_ALLOW, PROVIDER_SPECS

TEXT = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8")
RECOMMENDED = re.findall(r"^    - name: (\w+)$", TEXT.split("recommended_env:", 1)[1], re.M)
SECRET_ROWS = re.findall(r"^  \w+: \{type: secret, env: (\w+), description: \"([^\"]*)\"", TEXT, re.M)


def test_plugins_config_lists_keys_in_the_recommended_order():
    # `hermes plugins config web-search-plus` (Hermes v0.23.4) lists the secret
    # fields in manifest order; 5.1.9 started with Serper, not Brave.
    assert RECOMMENDED[:4] == ["BRAVE_API_KEY", "SERPER_API_KEY", "EXA_API_KEY", "LINKUP_API_KEY"]
    assert [env for env, _ in SECRET_ROWS][: len(RECOMMENDED)] == RECOMMENDED
    optional = re.findall(r"^  - (\w+)$", TEXT.split("optional_env:", 1)[1].split("provides_tools:", 1)[0], re.M)
    assert optional[: len(RECOMMENDED)] == RECOMMENDED


def test_explicit_only_hints_name_the_command_that_works():
    # 5.1.9 said "enable in config.json"; `config set-auto-allow X on` is the way.
    assert "enable in config.json" not in TEXT
    named = [p for _, text in SECRET_ROWS for p in re.findall(r"set-auto-allow (\w+) on", text)]
    assert sorted(named) == ["octen", "querit", "serpbase", "tinyfish"]
    for provider in named:
        assert provider in PROVIDER_SPECS and DEFAULT_AUTO_ALLOW.get(provider) is False


def test_onboarding_examples_match_the_routing():
    assert 'fixed_provider: "hermes web-search-plus config set-default brave"' in TEXT
    assert re.search(r'priority: "hermes web-search-plus config set-priority brave,', TEXT)
    assert "first for security and shopping" not in TEXT
