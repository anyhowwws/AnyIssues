import json
import unittest
from decimal import Decimal

from _harness import (
    FakeHTTP,
    assert_no_bare_reserved_words,
    callback_update,
    conditional_check_failed,
    load_handler,
    message_update,
    reset_module,
    webhook_event,
)

H = load_handler("webhook_handler")


def pending_draft(**overrides):
    # Exactly what boto3's resource layer hands back: numbers are Decimal.
    draft = {
        "draft_id": "abcd1234",
        "chat_id": Decimal(42),
        "message_id": Decimal(777),
        "status": "pending",
        "raw_text": "the raw thought",
        "llm_title": "Do the thing",
        "llm_body": "- AC one",
        "repos_snapshot": ["repo-a", "repo-b"],
        "created_at": Decimal(1),
    }
    draft.update(overrides)
    return draft


class WebhookTestCase(unittest.TestCase):
    def setUp(self):
        reset_module(H)
        self.http = FakeHTTP()
        self._patch = self.http.patch()
        self._patch.start()
        self.addCleanup(self._patch.stop)
        H.drafts_table.scan.return_value = {"Items": []}
        H.drafts_table.get_item.return_value = {"Item": pending_draft()}

    def invoke(self, update, **kw):
        return H.lambda_handler(webhook_event(update, **kw), None)

    def draft_puts(self):
        return [
            c.kwargs["Item"]
            for c in H.drafts_table.put_item.call_args_list
            if not c.kwargs["Item"]["draft_id"].startswith("upd:")
        ]

    def dedupe_puts(self):
        return [
            c.kwargs["Item"]
            for c in H.drafts_table.put_item.call_args_list
            if c.kwargs["Item"]["draft_id"].startswith("upd:")
        ]


class Gatekeeping(WebhookTestCase):
    def test_bad_secret_token_is_rejected(self):
        resp = self.invoke(message_update(1, "hi"), secret="wrong")
        self.assertEqual(resp["statusCode"], 401)
        self.assertEqual(self.http.calls, [])

    def test_other_users_are_ignored_silently(self):
        resp = self.invoke(message_update(1, "hi", user_id=999))
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.draft_puts(), [])

    def test_non_text_message_gets_a_hint(self):
        resp = self.invoke({"update_id": 1, "message": {"from": {"id": 42}, "chat": {"id": 42}, "voice": {}}})
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("Text only", self.http.sent("telegram", "sendMessage")[0]["text"])

    def test_base64_body_is_decoded(self):
        import base64

        raw = json.dumps(message_update(1, "hi")).encode()
        event = {
            "headers": {"x-telegram-bot-api-secret-token": "hook-secret"},
            "isBase64Encoded": True,
            "body": base64.b64encode(raw).decode(),
        }
        self.assertEqual(H.lambda_handler(event, None)["statusCode"], 200)
        self.assertEqual(len(self.draft_puts()), 1)


class NewMessage(WebhookTestCase):
    def test_creates_processing_draft_and_enqueues(self):
        self.invoke(message_update(1, "fix the login bug"))
        (draft,) = self.draft_puts()
        self.assertEqual(draft["status"], "processing")
        self.assertEqual(draft["raw_text"], "fix the login bug")
        self.assertEqual(draft["message_id"], 777)  # the placeholder Telegram returned
        self.assertGreater(draft["ttl"], draft["created_at"])
        body = json.loads(H.sqs.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual(body["draft_id"], draft["draft_id"])
        self.assertEqual(len(draft["draft_id"]), 8)

    def test_placeholder_sent_before_draft_written(self):
        self.invoke(message_update(1, "x"))
        self.assertEqual(self.http.sent("telegram", "sendMessage")[0]["text"], "\U0001f9e0 thinking...")


class Idempotency(WebhookTestCase):
    def test_every_update_writes_a_conditional_dedupe_marker(self):
        self.invoke(message_update(555, "x"))
        (marker,) = self.dedupe_puts()
        self.assertEqual(marker["draft_id"], "upd:555")
        call = [
            c
            for c in H.drafts_table.put_item.call_args_list
            if c.kwargs["Item"] is marker or c.kwargs["Item"]["draft_id"] == "upd:555"
        ][0]
        self.assertEqual(call.kwargs["ConditionExpression"], "attribute_not_exists(draft_id)")

    def test_duplicate_update_id_creates_exactly_one_draft(self):
        self.invoke(message_update(555, "x"))
        self.assertEqual(len(self.draft_puts()), 1)
        # Telegram redelivers: the marker now exists, so the conditional put fails.
        H.drafts_table.put_item.side_effect = conditional_check_failed()
        calls_before = len(self.http.calls)
        resp = self.invoke(message_update(555, "x"))
        self.assertEqual(resp["body"], "duplicate")
        self.assertEqual(len(self.draft_puts()), 1)
        self.assertEqual(len(self.http.calls), calls_before)
        self.assertEqual(H.sqs.send_message.call_count, 1)

    def test_duplicate_callback_is_dropped_too(self):
        self.invoke(callback_update(556, "d:abcd1234:u:asap"))
        H.drafts_table.put_item.side_effect = conditional_check_failed()
        calls_before = len(self.http.calls)
        self.invoke(callback_update(556, "d:abcd1234:u:asap"))
        self.assertEqual(len(self.http.calls), calls_before)

    def test_other_dedupe_errors_do_not_block_processing(self):
        from botocore.exceptions import ClientError

        H.drafts_table.put_item.side_effect = [
            ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem"),
            {},
        ]
        self.invoke(message_update(557, "x"))
        self.assertEqual(len(self.draft_puts()), 1)


class ButtonTaps(WebhookTestCase):
    def last_update_kwargs(self):
        return H.drafts_table.update_item.call_args.kwargs

    def test_project_tap_aliases_reserved_word_type(self):
        self.invoke(callback_update(1, "d:abcd1234:p:2"))  # index 2 == eureka
        kw = self.last_update_kwargs()
        self.assertEqual(kw["ExpressionAttributeNames"], {"#project": "project", "#type": "type"})
        assert_no_bare_reserved_words(self, kw["UpdateExpression"])
        self.assertEqual(kw["ExpressionAttributeValues"], {":project": "eureka", ":type": "eureka"})

    def test_eureka_hides_type_row_and_rerenders(self):
        self.invoke(callback_update(1, "d:abcd1234:p:2"))
        markup = self.http.sent("telegram", "editMessageReplyMarkup")[0]["reply_markup"]
        texts = [b["text"] for row in markup["inline_keyboard"] for b in row]
        self.assertNotIn("\U0001f41b bug", texts)
        self.assertIn("✅ eureka \U0001f9e0", texts)

    def test_urgency_tap_updates_and_rerenders_with_int_ids(self):
        self.invoke(callback_update(1, "d:abcd1234:u:asap"))
        kw = self.last_update_kwargs()
        self.assertEqual(kw["ExpressionAttributeValues"], {":v": "asap"})
        markup = self.http.sent("telegram", "editMessageReplyMarkup")[0]
        # Decimal chat_id/message_id from DynamoDB must serialise as plain ints.
        self.assertIs(type(markup["chat_id"]), int)
        self.assertIs(type(markup["message_id"]), int)

    def test_callback_data_stays_within_telegram_64_byte_cap(self):
        draft = pending_draft(repos_snapshot=[f"repo-{i}" for i in range(12)], project="eureka", type="eureka")
        for row in H.build_keyboard(draft)["inline_keyboard"]:
            for button in row:
                self.assertLessEqual(len(button["callback_data"].encode()), 64, button)

    def test_expired_draft_answers_with_alert(self):
        H.drafts_table.get_item.return_value = {}
        self.invoke(callback_update(1, "d:gone0000:u:asap"))
        answer = self.http.sent("telegram", "answerCallbackQuery")[0]
        self.assertTrue(answer["show_alert"])
        self.assertIn("expired", answer["text"])

    def test_discard_and_edit_change_status(self):
        self.invoke(callback_update(1, "d:abcd1234:x"))
        self.assertEqual(self.last_update_kwargs()["ExpressionAttributeValues"], {":s": "discarded"})
        self.invoke(callback_update(2, "d:abcd1234:e"))
        self.assertEqual(self.last_update_kwargs()["ExpressionAttributeValues"], {":s": "awaiting_edit"})

    def test_edit_correction_reuses_draft_and_aliases_ttl(self):
        H.drafts_table.scan.return_value = {"Items": [pending_draft(status="awaiting_edit")]}
        self.invoke(message_update(3, "actually it's a feature"))
        self.assertEqual(self.draft_puts(), [])  # no new draft
        kw = self.last_update_kwargs()
        self.assertEqual(kw["Key"], {"draft_id": "abcd1234"})
        self.assertIn("#ttl = :ttl", kw["UpdateExpression"])
        self.assertEqual(kw["ExpressionAttributeNames"]["#ttl"], "ttl")
        self.assertTrue(json.loads(H.sqs.send_message.call_args.kwargs["MessageBody"])["is_edit"])


class Confirm(WebhookTestCase):
    def ready(self, **overrides):
        fields = {"project": "repo-a", "type": "bug", "urgency": "asap", **overrides}
        H.drafts_table.get_item.return_value = {"Item": pending_draft(**fields)}

    def test_missing_fields_block_confirm(self):
        self.invoke(callback_update(1, "d:abcd1234:c"))
        answer = self.http.sent("telegram", "answerCallbackQuery")[0]
        self.assertEqual(answer["text"], "Pick project, urgency, type first.")
        self.assertEqual(self.http.sent("github"), [])

    def test_eureka_does_not_require_type(self):
        self.ready(project="eureka", type="eureka")
        self.invoke(callback_update(1, "d:abcd1234:c"))
        (issue,) = self.http.sent("github", "/issues")
        self.assertEqual(self.http.calls[-3][1], "/repos/owner/braindump/issues")
        self.assertEqual(issue["labels"], ["eureka", "urgency:asap"])

    def test_confirm_files_issue_and_records_url(self):
        self.ready()
        self.invoke(callback_update(1, "d:abcd1234:c"))
        (issue,) = self.http.sent("github", "/issues")
        self.assertEqual(issue["title"], "Do the thing")
        self.assertEqual(issue["labels"], ["bug", "urgency:asap"])
        self.assertIn("the raw thought", issue["body"])
        statuses = [c.kwargs["ExpressionAttributeValues"] for c in H.drafts_table.update_item.call_args_list]
        self.assertEqual(statuses[0], {":new": "confirming", ":pending": "pending"})
        self.assertEqual(statuses[1], {":s": "confirmed", ":u": "https://github.com/owner/repo-a/issues/1"})
        self.assertIn(
            "Filed: https://github.com/owner/repo-a/issues/1", self.http.sent("telegram", "editMessageText")[0]["text"]
        )

    def test_claim_is_conditional_on_pending(self):
        self.ready()
        self.invoke(callback_update(1, "d:abcd1234:c"))
        claim = H.drafts_table.update_item.call_args_list[0].kwargs
        self.assertEqual(claim["ConditionExpression"], "#s = :pending")

    def test_two_rapid_confirms_create_exactly_one_issue(self):
        self.ready()
        claims = []

        def update_item(**kw):
            if "ConditionExpression" in kw:
                claims.append(kw)
                if len(claims) > 1:
                    raise conditional_check_failed("UpdateItem")
            return {}

        H.drafts_table.update_item.side_effect = update_item
        self.invoke(callback_update(1, "d:abcd1234:c"))
        self.invoke(callback_update(2, "d:abcd1234:c"))
        self.assertEqual(len(self.http.sent("github", "/issues")), 1)
        answers = self.http.sent("telegram", "answerCallbackQuery")
        self.assertEqual(answers[-1]["text"], "Already filing this one.")

    def test_already_confirmed_draft_short_circuits(self):
        self.ready(status="confirmed")
        self.invoke(callback_update(1, "d:abcd1234:c"))
        self.assertEqual(self.http.sent("github"), [])
        self.assertEqual(self.http.sent("telegram", "answerCallbackQuery")[0]["text"], "Already filed.")

    def test_github_failure_releases_claim_for_retry(self):
        self.ready()
        self.http.fail["github"] = 502
        self.invoke(callback_update(1, "d:abcd1234:c"))
        statuses = [c.kwargs["ExpressionAttributeValues"] for c in H.drafts_table.update_item.call_args_list]
        self.assertEqual(statuses[-1], {":s": "pending"})
        answer = self.http.sent("telegram", "answerCallbackQuery")[0]
        self.assertTrue(answer["show_alert"])
        self.assertIn("retry", answer["text"])


class Resilience(WebhookTestCase):
    def test_telegram_error_still_returns_200(self):
        # A non-2xx would make Telegram redeliver the same update forever.
        self.http.fail["telegram"] = 400
        resp = self.invoke(message_update(1, "x"))
        self.assertEqual(resp["statusCode"], 200)

    def test_401_evicts_cached_param(self):
        self.invoke(message_update(1, "x"))
        self.assertIn("/anyissues-test/telegram-bot-token", H._cache)
        self.http.fail["telegram"] = 401
        self.invoke(message_update(2, "x"))
        self.assertNotIn("/anyissues-test/telegram-bot-token", H._cache)

    def test_json_default_handles_decimal_only(self):
        self.assertEqual(H._json_default(Decimal(5)), 5)
        self.assertEqual(H._json_default(Decimal("2.5")), 2.5)
        with self.assertRaises(TypeError):
            H._json_default(object())


if __name__ == "__main__":
    unittest.main()
