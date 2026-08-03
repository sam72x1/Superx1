from __future__ import annotations

import unittest

from services.kronos_inference.config import (
    DEFAULT_KRONOS_SOURCE_REVISION,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_REVISION,
    DEFAULT_TOKENIZER_ID,
    DEFAULT_TOKENIZER_REVISION,
    ConfigError,
    ServiceConfig,
)


class ConfigTests(unittest.TestCase):
    def test_security_and_weight_defaults_are_fixed(self) -> None:
        config = ServiceConfig.from_env({})
        self.assertEqual(config.host, "127.0.0.1")
        self.assertIsNone(config.api_token)
        self.assertEqual(config.max_request_threads, 16)
        self.assertEqual(config.kronos_source_revision, DEFAULT_KRONOS_SOURCE_REVISION)
        self.assertEqual(config.model_id, DEFAULT_MODEL_ID)
        self.assertEqual(config.model_revision, DEFAULT_MODEL_REVISION)
        self.assertEqual(config.tokenizer_id, DEFAULT_TOKENIZER_ID)
        self.assertEqual(config.tokenizer_revision, DEFAULT_TOKENIZER_REVISION)

    def test_token_whitespace_and_context_above_small_limit_are_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            ServiceConfig.from_env({"KRONOS_API_TOKEN": "secret token"})
        with self.assertRaises(ConfigError):
            ServiceConfig.from_env({"KRONOS_MAX_CONTEXT": "513"})

    def test_max_request_threads_can_be_bounded_explicitly(self) -> None:
        config = ServiceConfig.from_env({"KRONOS_MAX_REQUEST_THREADS": "8"})
        self.assertEqual(config.max_request_threads, 8)
        with self.assertRaises(ConfigError):
            ServiceConfig.from_env({"KRONOS_MAX_REQUEST_THREADS": "0"})

    def test_source_revision_can_be_overridden_explicitly(self) -> None:
        revision = "a" * 40
        config = ServiceConfig.from_env({"KRONOS_SOURCE_REVISION": revision})
        self.assertEqual(config.kronos_source_revision, revision)

    def test_floating_or_non_commit_revisions_are_rejected(self) -> None:
        for name in (
            "KRONOS_SOURCE_REVISION",
            "KRONOS_MODEL_REVISION",
            "KRONOS_TOKENIZER_REVISION",
        ):
            with self.subTest(name=name), self.assertRaises(ConfigError):
                ServiceConfig.from_env({name: "main"})

    def test_public_bind_requires_token(self) -> None:
        with self.assertRaisesRegex(ConfigError, "KRONOS_API_TOKEN"):
            ServiceConfig.from_env({"KRONOS_HOST": "0.0.0.0"})
        with self.assertRaisesRegex(ConfigError, "KRONOS_API_TOKEN"):
            ServiceConfig(host="0.0.0.0", api_token="")

        config = ServiceConfig.from_env(
            {"KRONOS_HOST": "0.0.0.0", "KRONOS_API_TOKEN": "secret"}
        )
        self.assertEqual(config.host, "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
