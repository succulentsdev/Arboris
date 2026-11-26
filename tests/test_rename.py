"""Tests for the GreenSearch -> Arboris rename (0.9.0) and its one-release
compatibility layer: the greensearch shim package, GREENSEARCH_* env
fallbacks, and the site's localStorage key migration.
"""

import importlib
import os
import subprocess
import sys
import unittest
import warnings
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arboris  # noqa: E402
from arboris.providers import OpenAICompatibleProvider  # noqa: E402
from server.app import Config, _with_legacy_env  # noqa: E402


class TestShimPackage(unittest.TestCase):
    def _import_shim(self):
        sys.modules.pop("greensearch", None)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            shim = importlib.import_module("greensearch")
        return shim, caught

    def test_shim_warns_and_shares_module_objects(self):
        shim, caught = self._import_shim()
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))
        self.assertEqual(shim.__version__, arboris.__version__)
        from greensearch import footprint as legacy_footprint
        self.assertIs(legacy_footprint, importlib.import_module("arboris.footprint"))
        self.assertIs(sys.modules["greensearch.providers"],
                      importlib.import_module("arboris.providers"))

    def test_legacy_module_entry_point_runs_the_cli(self):
        result = subprocess.run(
            [sys.executable, "-m", "greensearch", "Explain DNS simply", "--mode", "sprout"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DNS", result.stdout)


class TestLegacyEnv(unittest.TestCase):
    def test_legacy_name_fills_an_unset_new_name(self):
        merged, used = _with_legacy_env({"GREENSEARCH_MODEL": "m-old"})
        self.assertEqual(merged["ARBORIS_MODEL"], "m-old")
        self.assertEqual(used, ("GREENSEARCH_MODEL",))

    def test_new_name_always_wins(self):
        merged, used = _with_legacy_env(
            {"GREENSEARCH_MODEL": "m-old", "ARBORIS_MODEL": "m-new"})
        self.assertEqual(merged["ARBORIS_MODEL"], "m-new")
        self.assertEqual(used, ())

    def test_blank_new_name_falls_back(self):
        merged, used = _with_legacy_env(
            {"GREENSEARCH_MODEL": "m-old", "ARBORIS_MODEL": "  "})
        self.assertEqual(merged["ARBORIS_MODEL"], "m-old")
        self.assertEqual(used, ("GREENSEARCH_MODEL",))

    def test_config_reads_legacy_deployments(self):
        cfg = Config.from_env({"GREENSEARCH_GRID_G_PER_KWH": "30"})
        self.assertEqual(cfg.grid_g_per_kwh, 30.0)
        self.assertEqual(cfg.legacy_env_names, ("GREENSEARCH_GRID_G_PER_KWH",))

    def test_input_env_is_not_mutated(self):
        env = {"GREENSEARCH_MODEL": "m-old"}
        _with_legacy_env(env)
        self.assertEqual(env, {"GREENSEARCH_MODEL": "m-old"})

    def test_provider_accepts_legacy_api_key(self):
        with mock.patch.dict(os.environ, {"GREENSEARCH_API_KEY": "k-old"}, clear=True):
            provider = OpenAICompatibleProvider("https://example.org/v1")
        self.assertEqual(provider.api_key, "k-old")

    def test_provider_prefers_new_api_key(self):
        with mock.patch.dict(os.environ, {"GREENSEARCH_API_KEY": "k-old",
                                          "ARBORIS_API_KEY": "k-new"}, clear=True):
            provider = OpenAICompatibleProvider("https://example.org/v1")
        self.assertEqual(provider.api_key, "k-new")


class TestSiteStorageMigration(unittest.TestCase):
    def test_site_uses_new_keys_and_migrates_old_ones(self):
        # Live-only site: there is no demo STORE_KEY any more. Live keys
        # migrate from the old greensearch-* names, and old demo keys are
        # deleted on load instead of being migrated.
        html = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
        # Live state is per profile now: one history key per profile id.
        self.assertIn("var LIVE_STATE_KEY=profileKey(profileStore.current);", html)
        self.assertIn("function profileKey(id){return 'arboris-live-v1-'+id;}", html)
        self.assertIn("var PROFILE_STORE_KEY='arboris-profiles-v1';", html)
        # First run migrates the old single-history key into the Guest profile.
        self.assertIn("localStorage.getItem('arboris-live-v1')", html)
        self.assertIn("localStorage.setItem(profileKey('guest'),legacy)", html)
        self.assertIn("var LIVE_URL_KEY='arboris-live-backend-url';", html)
        self.assertIn("localStorage.getItem('greensearch-live-v1')", html)
        self.assertIn("localStorage.getItem('greensearch-live-backend-url')", html)
        self.assertIn("localStorage.removeItem('arboris-demo-v1')", html)
        self.assertIn("localStorage.removeItem('greensearch-demo-v1')", html)
        self.assertNotIn("var STORE_KEY='arboris-demo-v1';", html)
        self.assertNotIn("EXPANDED_TOPICS", html)


if __name__ == "__main__":
    unittest.main()
