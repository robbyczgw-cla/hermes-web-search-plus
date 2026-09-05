"""Portable adapter contracts; real Hermes discovery is tested separately."""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def backend(monkeypatch):
    abc = ModuleType("agent.web_search_provider")
    abc.WebSearchProvider = object
    web = ModuleType("tools.web_tools")
    config = {"backend": "wsp"}
    web._load_web_config = lambda: config
    monkeypatch.setitem(sys.modules, "agent.web_search_provider", abc)
    monkeypatch.setitem(sys.modules, "tools.web_tools", web)
    spec = importlib.util.spec_from_file_location(
        "_wsp_native_unit", Path(__file__).resolve().parents[1] / "native_backend.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = SimpleNamespace(
        load_config=lambda: {"auto_routing": {}},
        provider_configured=lambda *args: True,
        SEARCH_PROVIDER_IDS=("fixture",), EXTRACT_PROVIDER_IDS=("fixture",),
        _provider_auto_allowed=lambda *args: True,
    )
    plugin = SimpleNamespace(
        _force_subprocess=lambda: False, _load_search_module=lambda: engine,
        _run_search=Mock(return_value={"provider": "fixture", "results": [
            {"url": "https://example.org", "title": "Source", "snippet": "Evidence"}]}),
        _run_extract=Mock(return_value={"results": [
            {"url": "https://example.org", "title": "Source", "content": "Evidence"}]}),
    )
    return SimpleNamespace(module=module, value=module.WSPNativeBackend(plugin),
                           plugin=plugin, config=config, engine=engine)


def test_no_implicit_selection(backend):
    backend.config.clear()
    assert not backend.value.is_available()
    assert not backend.value.supports_search()
    assert not backend.value.supports_extract()


def test_independent_pins(backend):
    backend.config.update(search_backend="searxng", extract_backend="wsp")
    assert not backend.value.supports_search()
    assert backend.value.supports_extract()
    assert backend.value.is_available()


@pytest.mark.parametrize("bad", [[], ["wsp"], 4, {}, True])
def test_malformed_config(backend, bad):
    backend.module._load_web_config = lambda: {"search_backend": bad}
    assert not backend.value.supports_search()


def test_requires_exact_plugin(backend):
    with pytest.raises(RuntimeError):
        backend.module.WSPNativeBackend()


def test_readiness_without_vendor_calls(backend):
    assert backend.value.is_available()
    backend.plugin._run_search.assert_not_called()
    backend.plugin._run_extract.assert_not_called()
    backend.engine.provider_configured = lambda *args: False
    assert not backend.value.is_available()


def test_readiness_honors_disabled_and_auto_allow(backend):
    backend.engine.load_config = lambda: {"auto_routing": {"disabled_providers": ["fixture"]}}
    assert not backend.value.is_available()
    backend.engine.load_config = lambda: {"auto_routing": {}}
    backend.engine._provider_auto_allowed = lambda *args: False
    assert not backend.value.is_available()


@pytest.mark.parametrize("failure", ["forced", "missing", "raises"])
def test_no_subprocess_fallback(backend, failure):
    if failure == "forced":
        backend.plugin._force_subprocess = lambda: True
    elif failure == "missing":
        backend.plugin._load_search_module = lambda: None
    else:
        backend.plugin._load_search_module = Mock(side_effect=RuntimeError("private-data"))
    assert not backend.value.is_available()
    assert not backend.value.search("q")["success"]
    assert backend.value.extract(["https://example.org"])[0]["error"]
    backend.plugin._run_search.assert_not_called()
    backend.plugin._run_extract.assert_not_called()


def test_search_mapping_limit_and_no_fallback_flag(backend):
    result = backend.value.search("q", 35)
    assert result["success"]
    assert result["data"]["web"][0]["description"] == "Evidence"
    assert result["metadata"]["effective_limit"] == 20
    assert backend.plugin._run_search.call_args.kwargs["inprocess_only"] is True
    assert backend.plugin._run_search.call_args.kwargs["count"] == 20


@pytest.mark.parametrize("payload", [None, {}, {"results": {}}, {"data": []}, {"data": None}, {"error": "secret-dummy"}])
def test_search_malformed_response(backend, payload):
    backend.plugin._run_search.return_value = payload
    result = backend.value.search("q")
    assert result["success"] is False
    assert "secret-dummy" not in str(result)


def test_legacy_search_shape(backend):
    backend.plugin._run_search.return_value = {"data": {"web": [
        {"url": "https://example.org", "description": "legacy"}]}}
    assert backend.value.search("q")["data"]["web"][0]["description"] == "legacy"


def test_exception_redaction(backend, caplog):
    backend.plugin._run_search.side_effect = RuntimeError("secret-dummy")
    backend.plugin._run_extract.side_effect = RuntimeError("secret-dummy")
    assert "secret-dummy" not in str(backend.value.search("q"))
    assert "secret-dummy" not in str(backend.value.extract(["https://example.org"]))
    assert "secret-dummy" not in caplog.text


def test_extract_association_and_missing_failures(backend):
    backend.plugin._run_extract.return_value = {"results": [
        {"url": "https://foreign.invalid", "content": "foreign"},
        {"url": "https://second.invalid", "content": "second"},
        {"url": "https://example.org", "error": "secret-dummy"},
    ]}
    urls = ["https://example.org", "https://missing.invalid", "https://second.invalid"]
    result = backend.value.extract(urls)
    assert [r["url"] for r in result] == urls
    assert result[0]["error"] and result[1]["error"]
    assert result[2]["content"] == "second"
    assert "foreign" not in str(result) and "secret-dummy" not in str(result)
    assert backend.plugin._run_extract.call_args.kwargs["inprocess_only"] is True


def test_extract_format_metadata(backend):
    backend.plugin._run_extract.return_value = {"results": [
        {"url": "https://example.org", "markdown": "text", "truncated": True}]}
    row = backend.value.extract(["https://example.org"], format="html")[0]
    assert row["content"] == "text" and row["metadata"]["truncated"]
    assert backend.plugin._run_extract.call_args.kwargs["output_format"] == "html"


def test_registration_and_older_host(backend):
    handle = object()
    ctx = SimpleNamespace(register_web_search_provider=Mock(return_value=handle))
    assert backend.module.register_native_backend(ctx, backend.plugin) is handle
    registered = ctx.register_web_search_provider.call_args.args[0]
    assert registered.plugin is backend.plugin
    assert backend.module.register_native_backend(object(), backend.plugin) is None
    ctx.register_web_search_provider.side_effect = RuntimeError("failure")
    assert backend.module.register_native_backend(ctx, backend.plugin) is None
