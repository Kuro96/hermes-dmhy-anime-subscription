from pathlib import Path

import hermes_dmhy_anime_subscription as plugin_package


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_register_is_importable_and_callable():
    assert callable(plugin_package.register)


def test_plugin_metadata_matches_manifest_name():
    manifest = (REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8")

    assert 'name: dmhy-anime-subscription' in manifest
    assert 'version: "0.1.0"' in manifest
    assert 'provides_tools:' in manifest
    assert 'dmhy.run_once_dry_run' in manifest
    assert 'provides_hooks:' in manifest
    assert plugin_package.PLUGIN_METADATA["name"] == "dmhy-anime-subscription"
    assert "dmhy.run_once_dry_run" in plugin_package.PLUGIN_METADATA["provides_tools"]
