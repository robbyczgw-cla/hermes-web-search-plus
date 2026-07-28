from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "CONTRIBUTING.md"
README = ROOT / "README.md"

CI_COMMANDS = (
    "ruff check --config ruff.toml .",
    "python -m pytest tests/ -q",
    "python scripts/gen_provider_docs.py --check",
    "python scripts/gen_contract_v3_schemas.py --check",
    "node tests/schema_boundary_v3.mjs",
    "python -m compileall -q .",
)


def test_contributing_guide_exists_and_is_linked_from_readme():
    assert GUIDE.is_file()
    assert "[Contributing](CONTRIBUTING.md)" in README.read_text(encoding="utf-8")


def test_contributing_guide_tracks_the_ci_gate():
    guide = GUIDE.read_text(encoding="utf-8")
    for command in CI_COMMANDS:
        assert command in guide, command


def test_contributing_guide_covers_public_contract_and_hygiene():
    guide = GUIDE.read_text(encoding="utf-8")
    for required in (
        "source-only",
        "python setup.py new-provider",
        "docs/PROVIDER_SDK.md",
        "## [Unreleased]",
        "Do not include exploit details",
        "Do not bump versions",
    ):
        assert required in guide, required
