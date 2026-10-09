"""The production gate must act before fixture module code executes."""

from __future__ import annotations

from wsp_core.provider_registry import PROVIDER_SPECS, discover_providers


_NON_PRODUCTION_MODULE = """\
from pathlib import Path

from wsp_sdk import ProviderSpec

Path({marker!r}).write_text("executed", encoding="utf-8")

PROVIDER = ProviderSpec(
    id="gate-probe",
    kind="disabled",
    env_var="GATE_PROBE_KEY",
    display_name="Gate probe",
    description="Non-production gate probe fixture",
    config_section="gate_probe",
    signup_url="https://example.invalid",
    production=False,
)
"""


def test_non_production_module_is_not_executed_without_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("WSP_SDK_ALLOW_NON_PRODUCTION", raising=False)
    marker = tmp_path / "executed.marker"
    (tmp_path / "gate_probe.py").write_text(
        _NON_PRODUCTION_MODULE.format(marker=str(marker)), encoding="utf-8"
    )

    specs, diagnostics = discover_providers(tmp_path, existing_ids=PROVIDER_SPECS)

    assert specs == ()
    assert diagnostics == ()
    assert not marker.exists(), "non-production module code ran despite the gate"


def test_non_production_module_loads_with_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("WSP_SDK_ALLOW_NON_PRODUCTION", "1")
    marker = tmp_path / "executed.marker"
    (tmp_path / "gate_probe.py").write_text(
        _NON_PRODUCTION_MODULE.format(marker=str(marker)), encoding="utf-8"
    )

    specs, _diagnostics = discover_providers(tmp_path, existing_ids=PROVIDER_SPECS)

    assert [spec.provider for spec in specs] == ["gate-probe"]
    assert marker.exists()


def test_production_module_still_loads_without_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("WSP_SDK_ALLOW_NON_PRODUCTION", raising=False)
    (tmp_path / "prod_probe.py").write_text(
        "from wsp_sdk import ProviderSpec\n"
        "PROVIDER = ProviderSpec(id='prod-probe', kind='disabled', env_var='PROD_PROBE_KEY', "
        "display_name='Prod probe', description='Production probe fixture', "
        "config_section='prod_probe', signup_url='https://example.invalid')\n",
        encoding="utf-8",
    )

    specs, _diagnostics = discover_providers(tmp_path, existing_ids=PROVIDER_SPECS)

    assert [spec.provider for spec in specs] == ["prod-probe"]
