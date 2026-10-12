"""Unit tests for Antigravity model catalog caching and normalization."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from models import (
    clean_base_models,
    load_models_cache,
    models_cache_path,
    models_cache_ttl,
    save_models_cache,
)


class ModelCatalogCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_file = Path(self.temp_dir.name) / "models.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_clean_base_models_strips_gemini_effort_suffixes(self) -> None:
        raw = [
            "gemini-3.8-flash-low",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-high",
            "gemini-3.1-pro-high",
            "claude-sonnet-4-6",
            "claude-opus-4-6-thinking",
            "gpt-oss-120b-medium",
        ]
        cleaned = clean_base_models(raw)
        self.assertEqual(
            cleaned,
            [
                "gemini-3.8-flash",
                "gemini-3.1-pro",
                "claude-sonnet-4-6",
                "claude-opus-4-6-thinking",
                "gpt-oss-120b-medium",
            ],
        )

    def test_save_and_load_models_cache_roundtrip(self) -> None:
        with patch.dict("os.environ", {"ANTIGRAVITY_MODELS_CACHE_FILE": str(self.cache_file)}):
            self.assertIsNone(load_models_cache(3600.0))
            models = ["gemini-3.8-flash", "claude-sonnet-4-6"]
            save_models_cache(models)
            loaded = load_models_cache(3600.0)
            self.assertEqual(loaded, models)

    def test_load_models_cache_honors_ttl(self) -> None:
        with patch.dict("os.environ", {"ANTIGRAVITY_MODELS_CACHE_FILE": str(self.cache_file)}):
            models = ["gemini-3.8-flash"]
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps({"fetched_at": time.time() - 4000.0, "models": models}),
                encoding="utf-8",
            )
            # Expired with TTL 3600s
            self.assertIsNone(load_models_cache(3600.0))
            # Returns stale when max_age_seconds is None
            self.assertEqual(load_models_cache(None), models)

    def test_models_cache_ttl_env_override(self) -> None:
        with patch.dict("os.environ", {"ANTIGRAVITY_MODELS_CACHE_TTL": "1800"}):
            self.assertEqual(models_cache_ttl(), 1800.0)
        with patch.dict("os.environ", {"ANTIGRAVITY_MODELS_CACHE_TTL": "invalid"}):
            self.assertEqual(models_cache_ttl(), 3600.0)

    def test_corrupt_cache_file_returns_none(self) -> None:
        with patch.dict("os.environ", {"ANTIGRAVITY_MODELS_CACHE_FILE": str(self.cache_file)}):
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text("{corrupted json", encoding="utf-8")
            self.assertIsNone(load_models_cache(3600.0))
            self.assertIsNone(load_models_cache(None))
