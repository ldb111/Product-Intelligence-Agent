"""公共静态模型角色测试，所有凭据均为运行时生成的合成值。"""

import io
import traceback
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage

from event_core.schemas import DedupDecision
from llm.client import LLMCallError, LLMStructuredOutputError, get_llm
from llm.config import LLMConfigurationError, load_model_config


def environment():
    return {
        "LLM_PROVIDER": "fast_vendor", "LLM_BASE_URL": "https://fast.example.invalid/v1",
        "LLM_MODEL": "fast-model-from-env", "LLM_API_KEY": uuid4().hex,
        "LLM_REASONING_PROVIDER": "test_vendor", "LLM_REASONING_BASE_URL": "https://reasoning.example.invalid/v1",
        "LLM_REASONING_MODEL": "reasoning-model-from-env", "TEST_VENDOR_API_KEY": uuid4().hex,
    }


class ModelRoleTests(unittest.TestCase):
    def test_reasoning_provider_model_and_key_come_from_environment(self):
        env = environment()
        config = load_model_config("reasoning", env)
        self.assertEqual(config.provider, env["LLM_REASONING_PROVIDER"])
        self.assertEqual(config.model, env["LLM_REASONING_MODEL"])
        self.assertEqual(config.api_key, env["TEST_VENDOR_API_KEY"])
        self.assertNotIn(config.api_key, repr(config))

    def test_default_and_fast_preserve_existing_environment_contract(self):
        env = environment()
        for role in ("default", "fast"):
            config = load_model_config(role, env)
            self.assertEqual(config.model, env["LLM_MODEL"])
            self.assertEqual(config.api_key, env["LLM_API_KEY"])

    def test_missing_reasoning_fields_do_not_fall_back(self):
        for name in ("LLM_REASONING_PROVIDER", "LLM_REASONING_BASE_URL", "LLM_REASONING_MODEL", "TEST_VENDOR_API_KEY"):
            env = environment()
            del env[name]
            with self.subTest(name=name), self.assertRaises(LLMConfigurationError) as raised:
                load_model_config("reasoning", env)
            self.assertIn(name, str(raised.exception))

    def test_generic_reasoning_key_can_override_provider_key_without_dynamic_routing(self):
        env = environment()
        env["LLM_REASONING_API_KEY"] = uuid4().hex
        self.assertEqual(load_model_config("reasoning", env).api_key, env["LLM_REASONING_API_KEY"])

    def test_get_llm_passes_exact_config_to_existing_client(self):
        env = environment()
        with patch("llm.client.ChatOpenAI") as model:
            client = get_llm("reasoning", env)
        self.assertEqual(client.config.role, "reasoning")
        self.assertEqual(model.call_args.kwargs["model"], env["LLM_REASONING_MODEL"])
        self.assertEqual(model.call_args.kwargs["base_url"], env["LLM_REASONING_BASE_URL"])
        self.assertEqual(model.call_args.kwargs["max_retries"], 0)

    def test_secret_is_not_in_error_trace_or_console_output(self):
        env = environment()
        secret = env["TEST_VENDOR_API_KEY"]
        with patch("llm.client.ChatOpenAI") as model:
            model.return_value.with_structured_output.return_value.invoke.side_effect = RuntimeError(secret)
            client = get_llm("reasoning", env)
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                try:
                    client.decide("Judge", {}, DedupDecision)
                except LLMCallError:
                    traceback.print_exc()
            self.assertNotIn(secret, output.getvalue())
            self.assertIn("LLMCallError", output.getvalue())
            model.assert_called_once()

    def test_invalid_structured_output_fails_without_repair_or_fallback(self):
        with patch("llm.client.ChatOpenAI") as model:
            runnable = model.return_value.with_structured_output.return_value
            runnable.invoke.return_value = {"parsed": None, "parsing_error": ValueError("invalid")}
            client = get_llm("reasoning", environment())
            with self.assertRaises(LLMStructuredOutputError):
                client.decide("Judge", {}, DedupDecision)
            runnable.invoke.assert_called_once()

    def test_client_initialization_failure_does_not_expose_secret(self):
        env = environment()
        secret = env["TEST_VENDOR_API_KEY"]
        with patch("llm.client.ChatOpenAI", side_effect=ValueError(secret)):
            with self.assertRaises(LLMCallError) as raised:
                get_llm("reasoning", env)
        self.assertNotIn(secret, str(raised.exception))

    def test_runtime_record_contains_reported_model_and_usage(self):
        with patch("llm.client.ChatOpenAI") as model:
            model.return_value.with_structured_output.return_value.invoke.return_value = {
                "parsed": DedupDecision(decision="NEW_EVENT", reason="Independent change."),
                "parsing_error": None,
                "raw": AIMessage(content="{}", response_metadata={"model_name": "reported-model"},
                                 usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
            }
            client = get_llm("reasoning", environment())
            result, record = client.decide("Judge", {}, DedupDecision)
        self.assertEqual(result.decision, "NEW_EVENT")
        self.assertEqual(record.response_model, "reported-model")
        self.assertEqual(record.token_usage["total_tokens"], 15)

    def test_sensitive_url_is_rejected_without_echoing_it(self):
        env = environment()
        secret = env["TEST_VENDOR_API_KEY"]
        env["LLM_REASONING_BASE_URL"] = "https://example.invalid/?token=" + secret
        with self.assertRaises(LLMConfigurationError) as raised:
            load_model_config("reasoning", env)
        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
