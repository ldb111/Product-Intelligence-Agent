"""专用模型 HTTP 契约测试，凭据为临时合成值。真实调用另由 E2E 验证。"""

import io
import traceback
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch
from uuid import uuid4

import requests

from llm.config import LLMConfigurationError
from llm.retrieval_models import (
    EmbeddingAPIError, RerankAPIError, get_embedding, get_rerank, load_retrieval_config,
)


def environment():
    return {"LLM_PROVIDER": "aliyun_bailian", "LLM_API_KEY": uuid4().hex,
            "LLM_BASE_URL": "https://example.invalid/compatible-mode/v1",
            "EMBEDDING_PROVIDER": "aliyun_bailian", "EMBEDDING_MODEL": "fixture-embedding-from-env",
            "RERANK_PROVIDER": "aliyun_bailian", "RERANK_MODEL": "fixture-rerank-from-env"}


def response(payload, status=200):
    result = Mock(status_code=status)
    result.json.return_value = payload
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class RetrievalModelTests(unittest.TestCase):
    def test_roles_use_env_models_and_reuse_matching_existing_credential(self):
        env = environment()
        for role in ("embedding", "rerank"):
            config = load_retrieval_config(role, env)
            self.assertEqual(config.model, env[role.upper() + "_MODEL"])
            self.assertEqual(config.api_key, env["LLM_API_KEY"])
            self.assertNotIn(env["LLM_API_KEY"], repr(config))

    def test_missing_role_config_fails_without_chat_fallback(self):
        for key in ("EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "RERANK_PROVIDER", "RERANK_MODEL", "LLM_API_KEY", "LLM_BASE_URL"):
            env = environment()
            del env[key]
            role = "rerank" if key.startswith("RERANK") else "embedding"
            with self.subTest(key=key), self.assertRaises(LLMConfigurationError):
                load_retrieval_config(role, env)

    def test_credential_cannot_be_borrowed_from_different_provider(self):
        env = environment()
        env["LLM_PROVIDER"] = "different_vendor"
        with self.assertRaises(LLMConfigurationError):
            load_retrieval_config("embedding", env)

    def test_provider_credential_and_optional_base_are_supported(self):
        env = environment()
        env.update(LLM_PROVIDER="different_vendor", ALIYUN_BAILIAN_API_KEY=uuid4().hex,
                   ALIYUN_BAILIAN_BASE_URL="https://example.invalid/compatible-mode/v1")
        self.assertEqual(load_retrieval_config("rerank", env).api_key, env["ALIYUN_BAILIAN_API_KEY"])
        env["RERANK_BASE_URL"] = "https://example.invalid/custom/v1"
        self.assertEqual(load_retrieval_config("rerank", env).endpoint, "https://example.invalid/custom/v1/reranks")

    def test_unsupported_provider_and_sensitive_or_invalid_url_fail(self):
        for update in ({"EMBEDDING_PROVIDER": "other"}, {"LLM_BASE_URL": "https://user:secret@example.invalid"},
                       {"LLM_BASE_URL": "http://example.invalid"}, {"LLM_BASE_URL": "https://example.invalid/?key=secret"},
                       {"LLM_BASE_URL": "https://example.invalid:bad"}):
            with self.subTest(update=update), self.assertRaises(LLMConfigurationError):
                load_retrieval_config("embedding", {**environment(), **update})

    @patch("llm.retrieval_models.requests.post")
    def test_embedding_http_body_order_usage_and_default_dimension(self, post):
        post.return_value = response({"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}],
                                      "model": "reported-fixture", "usage": {"total_tokens": 4}, "id": "fixture-request"})
        env = environment()
        output = get_embedding(env).embed(["甲", "乙"])
        self.assertEqual(output.vectors, [[1, 0], [0, 1]])
        self.assertEqual(post.call_args.kwargs["json"], {"model": env["EMBEDDING_MODEL"], "input": ["甲", "乙"], "encoding_format": "float"})
        self.assertTrue(post.call_args.args[0].endswith("/compatible-mode/v1/embeddings"))
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(output.runtime[0].response_model, "reported-fixture")
        self.assertEqual(output.runtime[0].usage, {"total_tokens": 4})

    @patch("llm.retrieval_models.requests.post")
    def test_embedding_batches_ten_and_rejects_partial_second_batch(self, post):
        post.side_effect = lambda *a, **kw: response({"data": [{"index": i, "embedding": [1, 0]} for i in range(len(kw["json"]["input"]))]})
        result = get_embedding(environment()).embed(["Fixture"] * 23)
        self.assertEqual(len(result.vectors), 23)
        self.assertEqual([c.input_count for c in result.runtime], [10, 10, 3])
        post.side_effect = [response({"data": [{"index": i, "embedding": [1, 0]} for i in range(10)]}), response({"data": []})]
        with self.assertRaises(EmbeddingAPIError):
            get_embedding(environment()).embed(["Fixture"] * 11)

    @patch("llm.retrieval_models.requests.post")
    def test_invalid_embeddings_do_not_become_fake_vectors(self, post):
        for payload in ({"data": []}, {"data": [{"index": 2, "embedding": [1]}]},
                        {"data": [{"index": 0, "embedding": [0, 0]}]},
                        {"data": [{"index": 0, "embedding": [float("nan")]}]},
                        {"data": [{"index": 0, "embedding": [True]}]}):
            post.return_value = response(payload)
            with self.subTest(payload=payload), self.assertRaises(EmbeddingAPIError):
                get_embedding(environment()).embed(["Fixture"])

    @patch("llm.retrieval_models.requests.post")
    def test_duplicate_embedding_indexes_are_rejected(self, post):
        post.return_value = response({"data": [{"index": 0, "embedding": [1]}, {"index": 0, "embedding": [1]}]})
        with self.assertRaises(EmbeddingAPIError):
            get_embedding(environment()).embed(["A", "B"])

    @patch("llm.retrieval_models.requests.post")
    def test_rerank_flat_protocol_and_ignores_model_generated_url(self, post):
        post.return_value = response({"results": [{"index": 1, "relevance_score": 0.8,
                                                  "document": {"text": "假的正文", "url": "https://invented.invalid"}}]})
        env = environment()
        output = get_rerank(env).rerank("问句", ["A", "B"], 1)
        self.assertEqual(output.items[0].index, 1)
        self.assertNotIn("invented", repr(output))
        self.assertEqual(post.call_args.kwargs["json"], {"model": env["RERANK_MODEL"], "query": "问句", "documents": ["A", "B"], "top_n": 1})
        self.assertTrue(post.call_args.args[0].endswith("/compatible-api/v1/reranks"))

    @patch("llm.retrieval_models.requests.post")
    def test_invalid_rerank_indexes_scores_and_count_fail(self, post):
        for rows in ([], [{"index": 3, "relevance_score": 0.5}], [{"index": 0, "relevance_score": float("inf")}],
                     [{"index": 0, "relevance_score": -1}], [{"index": True, "relevance_score": 0.5}]):
            post.return_value = response({"results": rows})
            with self.subTest(rows=rows), self.assertRaises(RerankAPIError):
                get_rerank(environment()).rerank("Fixture", ["A"], 1)

    @patch("llm.retrieval_models.requests.post")
    def test_api_and_json_failures_are_typed_and_do_not_retry(self, post):
        for make_client, call, error in ((get_embedding, lambda c: c.embed(["A"]), EmbeddingAPIError),
                                         (get_rerank, lambda c: c.rerank("Q", ["A"], 1), RerankAPIError)):
            for status in (302, 400, 401, 429, 500):
                post.reset_mock()
                post.return_value = response({}, status)
                with self.subTest(status=status, error=error), self.assertRaises(error):
                    call(make_client(environment()))
                post.assert_called_once()
            post.return_value = response({"error": {"message": "fixture"}})
            with self.assertRaises(error):
                call(make_client(environment()))
            post.return_value = response({})
            post.return_value.json.side_effect = ValueError("malformed json")
            with self.assertRaises(error):
                call(make_client(environment()))

    @patch("llm.retrieval_models.requests.post")
    def test_request_exception_secret_not_in_traceback_or_logs(self, post):
        env = environment()
        post.side_effect = requests.RequestException(env["LLM_API_KEY"])
        output = io.StringIO()
        with redirect_stderr(output):
            try:
                get_embedding(env).embed(["A"])
            except EmbeddingAPIError:
                traceback.print_exc()
        self.assertNotIn(env["LLM_API_KEY"], output.getvalue())
        self.assertIn("EmbeddingAPIError", output.getvalue())


if __name__ == "__main__":
    unittest.main()
