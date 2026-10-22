import json
import unittest
from decimal import Decimal

from _harness import FakeHTTP, assert_no_bare_reserved_words, llm_response, load_handler, reset_module, wif_enabled

S = load_handler("shape_worker")


def processing_draft(**overrides):
    draft = {
        "draft_id": "abcd1234",
        "chat_id": Decimal(42),
        "message_id": Decimal(777),
        "status": "processing",
        "raw_text": "login is broken_on *mobile* [again]",
        "created_at": Decimal(1),
    }
    draft.update(overrides)
    return draft


def sqs_event(draft_id="abcd1234"):
    return {"Records": [{"body": json.dumps({"draft_id": draft_id, "is_edit": False})}]}


class ShapeWorkerTestCase(unittest.TestCase):
    def setUp(self):
        reset_module(S)
        self.http = FakeHTTP()
        self._patch = self.http.patch()
        self._patch.start()
        self.addCleanup(self._patch.stop)
        S.drafts_table.get_item.return_value = {"Item": processing_draft()}

    def run_worker(self):
        return S.lambda_handler(sqs_event(), None)

    def preview(self):
        return self.http.sent("telegram", "editMessageText")[-1]


class HappyPath(ShapeWorkerTestCase):
    def test_writes_shaped_draft_with_aliased_reserved_words(self):
        self.run_worker()
        kw = S.drafts_table.update_item.call_args.kwargs
        self.assertEqual(kw["ExpressionAttributeValues"][":t"], "Do the thing")
        self.assertEqual(kw["ExpressionAttributeValues"][":s"], "pending")
        self.assertEqual(kw["ExpressionAttributeValues"][":r"], ["repo-a", "repo-b"])
        expected_names = {"#s": "status", "#ty": "type", "#ttl": "ttl", "#p": "project"}
        self.assertEqual(kw["ExpressionAttributeNames"], expected_names)
        assert_no_bare_reserved_words(self, kw["UpdateExpression"])
        self.assertIn("REMOVE #p, #ty, urgency", kw["UpdateExpression"])

    def test_preview_is_html_with_keyboard(self):
        self.run_worker()
        preview = self.preview()
        self.assertEqual(preview["parse_mode"], "HTML")
        self.assertIs(type(preview["chat_id"]), int)
        self.assertIn("<b>Do the thing</b>", preview["text"])
        rows = preview["reply_markup"]["inline_keyboard"]
        self.assertEqual([b["text"] for b in rows[0]], ["repo-a", "repo-b"])
        self.assertEqual(rows[-1][0]["callback_data"], "d:abcd1234:c")

    def test_anthropic_request_uses_structured_outputs(self):
        self.run_worker()
        (req,) = self.http.sent("anthropic")
        self.assertEqual(req["model"], "test-model")
        self.assertEqual(req["max_tokens"], 2000)
        fmt = req["output_config"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertEqual(fmt["schema"]["required"], ["title", "body"])
        self.assertFalse(fmt["schema"]["additionalProperties"])
        self.assertEqual(req["messages"], [{"role": "user", "content": "login is broken_on *mobile* [again]"}])

    def test_github_topic_search_query(self):
        self.run_worker()
        path = [p for h, p, _m, _b, _hdrs in self.http.calls if h == "github"][0]
        self.assertEqual(
            path, "/search/repositories?q=user%3Aowner%20archived%3Afalse%20fork%3Afalse&sort=updated&per_page=100"
        )

    def test_braindump_is_never_offered_as_a_project_button(self):
        self.http.github = lambda p, b: {"items": [{"name": "repo-a"}, {"name": "braindump"}, {"name": "repo-b"}]}
        self.run_worker()
        kw = S.drafts_table.update_item.call_args.kwargs
        self.assertEqual(kw["ExpressionAttributeValues"][":r"], ["repo-a", "repo-b"])

    def test_missing_draft_is_skipped(self):
        S.drafts_table.get_item.return_value = {}
        self.run_worker()
        self.assertEqual(self.http.calls, [])


class MarkdownSafety(ShapeWorkerTestCase):
    def test_llm_output_with_markdown_specials_is_html_escaped(self):
        self.http.anthropic = lambda p, b: llm_response(
            json.dumps({"title": "Fix *bold* and [x]_y", "body": "- item_1 <script>&"})
        )
        self.run_worker()
        text = self.preview()["text"]
        self.assertIn("<b>Fix *bold* and [x]_y</b>", text)
        self.assertIn("- item_1 &lt;script&gt;&amp;", text)
        self.assertIn("<i>raw: login is broken_on *mobile* [again]</i>", text)
        self.assertNotIn('parse_mode": "Markdown', json.dumps(self.preview()))

    def test_oversized_body_is_trimmed_outside_tags(self):
        text = S.preview_text("T", "x" * 10000, "raw")
        self.assertLessEqual(len(text), S.TELEGRAM_TEXT_LIMIT)
        self.assertTrue(text.startswith("<b>T</b>\n\n"))
        self.assertTrue(text.endswith("<i>raw: raw</i>"))
        self.assertIn("…", text)


class LlmParsing(ShapeWorkerTestCase):
    def test_tolerates_fenced_and_prose_wrapped_json(self):
        self.assertEqual(S._extract_ticket('```json\n{"title":"a","body":"b"}\n```'), ("a", "b"))
        self.assertEqual(S._extract_ticket('Sure: {"title":"a","body":"b"} done'), ("a", "b"))
        self.assertEqual(S._extract_ticket('{"title":"  a  ","body":"b"}'), ("a", "b"))

    def test_rejects_garbage(self):
        for bad in ["nope", "{", '{"title": 1, "body": "x"}', '{"body": "x"}', '{"title": "", "body": "x"}', "[1,2]"]:
            with self.subTest(bad=bad), self.assertRaises(S.ShapeError):
                S._extract_ticket(bad)

    def test_bad_output_marks_draft_failed_and_tells_user(self):
        self.http.anthropic = lambda p, b: llm_response("I cannot produce JSON right now")
        self.run_worker()  # must not raise: it would only make SQS retry a permanent failure
        kw = S.drafts_table.update_item.call_args.kwargs
        self.assertEqual(kw["ExpressionAttributeValues"], {":s": "failed"})
        self.assertIn("Couldn't shape", self.preview()["text"])
        self.assertEqual(self.http.sent("github"), [])

    def test_refusal_and_truncation_are_permanent_failures(self):
        for stop in ("refusal", "max_tokens"):
            with self.subTest(stop=stop):
                self.http.anthropic = lambda p, b, stop=stop: llm_response('{"title":"a","body":"b"}', stop_reason=stop)
                self.run_worker()
                self.assertEqual(
                    S.drafts_table.update_item.call_args.kwargs["ExpressionAttributeValues"], {":s": "failed"}
                )

    def test_4xx_is_permanent_but_5xx_and_429_retry_via_sqs(self):
        self.http.fail["anthropic"] = 400
        self.run_worker()
        self.assertEqual(S.drafts_table.update_item.call_args.kwargs["ExpressionAttributeValues"], {":s": "failed"})
        for status in (429, 503):
            with self.subTest(status=status):
                self.http.fail["anthropic"] = status
                with self.assertRaises(S.TransientError):
                    self.run_worker()

    def test_401_evicts_cached_api_key(self):
        S._cache["/anyissues-test/anthropic-api-key"] = "stale"
        self.http.fail["anthropic"] = 401
        self.run_worker()
        self.assertNotIn("/anyissues-test/anthropic-api-key", S._cache)


class RepoSearch(ShapeWorkerTestCase):
    def test_search_failure_degrades_to_eureka_only(self):
        self.http.fail["github"] = 403
        self.run_worker()
        kw = S.drafts_table.update_item.call_args.kwargs
        self.assertEqual(kw["ExpressionAttributeValues"][":r"], [])
        rows = self.preview()["reply_markup"]["inline_keyboard"]
        self.assertEqual(rows[0][0]["text"], "eureka \U0001f9e0")
        self.assertNotIn("/anyissues-test/github-pat", S._cache)


class WorkloadIdentityFederation(ShapeWorkerTestCase):
    def test_default_state_uses_the_ssm_key_not_wif(self):
        # Sanity check for every other test in this suite: with the WIF
        # constants unset (the harness default), nothing calls STS at all.
        self.run_worker()
        S.sts.get_web_identity_token.assert_not_called()
        (headers,) = self.http.headers_sent("anthropic", "/messages")
        self.assertIn("x-api-key", headers)
        self.assertNotIn("authorization", headers)

    def test_wif_used_when_configured(self):
        with wif_enabled(S):
            self.run_worker()
        S.sts.get_web_identity_token.assert_called_once_with(
            Audience=["https://api.anthropic.com"], SigningAlgorithm="RS256", DurationSeconds=900
        )
        (oauth_req,) = self.http.sent("anthropic", "/oauth/token")
        self.assertEqual(oauth_req["assertion"], "eyJ.fake-jwt.sig")
        self.assertEqual(oauth_req["federation_rule_id"], "fdrl_test")
        self.assertEqual(oauth_req["organization_id"], "org_test")
        self.assertEqual(oauth_req["service_account_id"], "svac_test")
        self.assertNotIn("workspace_id", oauth_req)
        (headers,) = self.http.headers_sent("anthropic", "/messages")
        self.assertEqual(headers["authorization"], "Bearer sk-ant-oat01-test")
        self.assertNotIn("x-api-key", headers)
        # The SSM key was never touched -- WIF succeeded, so no fallback.
        self.assertNotIn("/anyissues-test/anthropic-api-key", S._cache)

    def test_workspace_id_included_when_set(self):
        S.ANTHROPIC_WORKSPACE_ID = "wrkspc_test"
        self.addCleanup(setattr, S, "ANTHROPIC_WORKSPACE_ID", None)
        with wif_enabled(S):
            self.run_worker()
        (oauth_req,) = self.http.sent("anthropic", "/oauth/token")
        self.assertEqual(oauth_req["workspace_id"], "wrkspc_test")

    def test_token_is_cached_across_calls_within_one_container(self):
        with wif_enabled(S):
            self.run_worker()
            S.drafts_table.get_item.return_value = {"Item": processing_draft()}
            self.run_worker()
        self.assertEqual(S.sts.get_web_identity_token.call_count, 1)
        self.assertEqual(len(self.http.sent("anthropic", "/oauth/token")), 1)
        self.assertEqual(len(self.http.sent("anthropic", "/messages")), 2)

    def test_expired_token_is_refreshed(self):
        with wif_enabled(S):
            self.run_worker()
            S._wif["expires_at"] = S.time.time() - 1  # force expiry
            S.drafts_table.get_item.return_value = {"Item": processing_draft()}
            self.run_worker()
        self.assertEqual(S.sts.get_web_identity_token.call_count, 2)

    def test_missing_boto3_method_falls_back_to_ssm_key(self):
        S.sts.get_web_identity_token.side_effect = AttributeError(
            "'STS' object has no attribute 'get_web_identity_token'"
        )
        with wif_enabled(S):
            self.run_worker()
        self.assertEqual(self.http.sent("anthropic", "/oauth/token"), [])
        (headers,) = self.http.headers_sent("anthropic", "/messages")
        self.assertIn("x-api-key", headers)

    def test_oauth_exchange_failure_falls_back_to_ssm_key(self):
        self.http.oauth_fail = 401
        with wif_enabled(S):
            self.run_worker()
        (headers,) = self.http.headers_sent("anthropic", "/messages")
        self.assertIn("x-api-key", headers)
        self.assertNotIn("authorization", headers)
        kw = S.drafts_table.update_item.call_args.kwargs
        self.assertEqual(kw["ExpressionAttributeValues"][":s"], "pending")  # shaping still succeeded

    def test_401_from_anthropic_evicts_both_caches(self):
        with wif_enabled(S):
            self.run_worker()
            self.assertIn("access_token", S._wif)
            self.http.fail["anthropic"] = 401
            self.run_worker()
        self.assertNotIn("access_token", S._wif)


if __name__ == "__main__":
    unittest.main()
