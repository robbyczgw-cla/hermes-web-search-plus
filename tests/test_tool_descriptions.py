"""Hermes Tool Search shows only the first sentence of a tool description (about 60 characters)."""

import pytest

from plugin_loader import load_plugin

plugin = load_plugin("wsp_plugin_test_tool_descriptions")


class _Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def _descriptions():
    ctx = _Ctx()
    plugin.register(ctx)
    return {name: kwargs["schema"]["description"] for name, kwargs in ctx.tools.items()}


@pytest.mark.parametrize("name", ["web_search_plus", "web_extract_plus"])
def test_first_sentence_fits_the_tool_catalog(name):
    first_sentence = _descriptions()[name].split(". ")[0] + "."

    assert len(first_sentence) <= 60, first_sentence


def test_extract_description_lists_providers_in_default_order():
    description = _descriptions()["web_extract_plus"]

    assert (
        "Auto tries Tavily, Exa, Linkup, Parallel, Firecrawl, You.com, Keenable, Serper "
        "in that order (keyless Keenable only when its public endpoint is opted in)"
    ) in description
