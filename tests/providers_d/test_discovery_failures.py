from __future__ import annotations

import pytest

from wsp_core.provider_registry import PROVIDER_SPECS, discover_providers
from wsp_sdk import DuplicateProviderError


def test_broken_or_missing_provider_module_is_a_typed_startup_diagnostic(tmp_path):
    (tmp_path / "syntax_error.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "no_provider.py").write_text("VALUE = 1\n", encoding="utf-8")

    specs, diagnostics = discover_providers(tmp_path, existing_ids=PROVIDER_SPECS)

    assert specs == ()
    assert [(item.module, item.code) for item in diagnostics] == [
        ("no_provider", "missing_PROVIDER"),
        ("syntax_error", "module_load_failed"),
    ]


def test_duplicate_provider_id_fails_closed(tmp_path):
    (tmp_path / "duplicate.py").write_text(
        "from wsp_sdk import ProviderSpec\n"
        "PROVIDER = ProviderSpec(id='serper', kind='disabled', env_var='DUPLICATE_KEY', "
        "display_name='Duplicate', description='Duplicate fixture', config_section='duplicate', "
        "signup_url='https://example.invalid')\n",
        encoding="utf-8",
    )

    with pytest.raises(DuplicateProviderError, match="duplicate provider id: serper"):
        discover_providers(tmp_path, existing_ids=PROVIDER_SPECS)
