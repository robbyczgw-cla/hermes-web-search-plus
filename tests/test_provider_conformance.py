from __future__ import annotations

from wsp_sdk.conformance import provider_conformance_errors


def test_every_builtin_and_discovered_provider_passes_the_same_conformance_suite():
    assert provider_conformance_errors() == ()
