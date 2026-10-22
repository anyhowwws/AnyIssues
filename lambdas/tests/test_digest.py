import io
import json
import unittest
import urllib.error
from unittest import mock

from _harness import FakeHTTP, llm_response, load_handler, reset_module, wif_enabled

D = load_handler("digest")


def gh_issue(repo, number, title, labels=(), pull_request=False):
    item = {
        "number": number,
        "title": title,
        "labels": [{"name": name} for name in labels],
        "html_url": f"https://github.com/owner/{repo}/issues/{number}",
        "updated_at": "2026-09-01T10:00:00Z",
    }
    if pull_request:
        item["pull_request"] = {"url": "..."}
    return item


class GitHubStub:
    """Routes the digest's GitHub GETs: topic search, then per-repo issue pages."""

    def __init__(self, repos, issues_by_repo, pages=None):
        self.repos = repos
        self.issues_by_repo = issues_by_repo
        self.pages = pages or {}  # repo -> list of pages (each a list of items)
        self.paths = []

    def __call__(self, path, body):
        self.paths.append(path)
        if path.startswith("/search/repositories"):
            return {"items": [{"name": r} for r in self.repos]}
        repo = path.split("/repos/owner/")[1].split("/")[0]
        if repo in self.pages:
            page_no = int(path.split("page=")[-1]) if "&page=" in path else 1
            return self.pages[repo][page_no - 1]
        return self.issues_by_repo.get(repo, [])


class DigestTestCase(unittest.TestCase):
    def setUp(self):
        reset_module(D)
        self.http = FakeHTTP()
        self._patch = self.http.patch()
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.github = GitHubStub(
            repos=["repo-a", "repo-b"],
            issues_by_repo={
                "repo-a": [
                    gh_issue("repo-a", 1, "Fix login <crash>", ["bug", "urgency:asap"]),
                    gh_issue("repo-a", 2, "Some PR", ["feature"], pull_request=True),
                ],
                "repo-b": [gh_issue("repo-b", 7, "Add dark mode", ["feature", "urgency:good-to-have"])],
                "braindump": [gh_issue("braindump", 3, "App idea: plant waterer", ["eureka", "urgency:normal"])],
            },
        )
        self.http.github = self.github
        self.http.anthropic = lambda p, b: llm_response(
            "asap: repo-a#1 is the login crash.\nnormal: braindump#3 plant idea.\n"
            "good-to-have: repo-b#7 dark mode.\nWhich one do you want to start first?"
        )

    def telegram_texts(self, method):
        return [m["text"] for m in self.http.sent("telegram", method)]


class Collection(DigestTestCase):
    def test_reads_tracked_repos_plus_braindump_and_drops_pull_requests(self):
        issues = D.collect_issues()
        self.assertEqual([D.issue_ref(i) for i in issues], ["repo-a#1", "repo-b#7", "braindump#3"])
        self.assertIn(
            "/search/repositories?q=user%3Aowner%20archived%3Afalse%20fork%3Afalse&sort=updated&per_page=100",
            self.github.paths,
        )
        self.assertIn("/repos/owner/braindump/issues?state=open&per_page=100", self.github.paths)

    def test_braindump_not_duplicated_when_search_already_returns_it(self):
        self.github.repos = ["repo-a", "braindump"]
        self.assertEqual(D.fetch_tracked_repos(), ["repo-a", "braindump"])

    def test_follows_link_header_pagination(self):
        page1 = [gh_issue("repo-a", n, f"issue {n}") for n in range(1, 4)]
        page2 = [gh_issue("repo-a", n, f"issue {n}") for n in range(4, 6)]
        self.github.pages = {"repo-a": [page1, page2]}
        links = iter(
            ['<https://api.github.com/repos/owner/repo-a/issues?state=open&per_page=100&page=2>; rel="next"', ""]
        )
        real_get = D._github_get

        def get_with_link(path):
            data, _ = real_get(path)
            return data, next(links)

        with mock.patch.object(D, "_github_get", get_with_link):
            issues = D.fetch_open_issues("repo-a")
        self.assertEqual([i["number"] for i in issues], [1, 2, 3, 4, 5])


class Summary(DigestTestCase):
    def test_grouping_and_llm_input(self):
        text = D.issues_as_text(D.collect_issues())
        self.assertEqual(
            text.split("\n"),
            [
                "[repo-a]",
                "- repo-a#1 (bug, asap): Fix login <crash>  (updated 2026-09-01)",
                "[repo-b]",
                "- repo-b#7 (feature, good-to-have): Add dark mode  (updated 2026-09-01)",
                "[braindump]",
                "- braindump#3 (eureka, normal): App idea: plant waterer  (updated 2026-09-01)",
            ],
        )

    def test_ordered_repos_ranks_by_asap_then_normal_then_good_to_have_with_braindump_last(self):
        raw = [
            ("repo-a", ["urgency:good-to-have"]),
            ("repo-b", ["urgency:asap"]),
            ("repo-b", ["urgency:asap"]),
            ("repo-c", ["urgency:asap"]),
            ("repo-c", ["urgency:normal"]),
            ("braindump", ["urgency:asap"]),
        ]
        issues = [{"repo": repo, "number": n, "labels": labels} for n, (repo, labels) in enumerate(raw, start=1)]
        self.assertEqual(D.ordered_repos(issues), ["repo-b", "repo-c", "repo-a", "braindump"])

    def test_digest_is_html_with_links_and_header(self):
        digest = D.build_digest(D.collect_issues())
        self.assertTrue(digest.startswith("\U0001f4cb <b>3 open issues</b> across 3 repos\n\n"))
        self.assertIn('<a href="https://github.com/owner/repo-a/issues/1">repo-a#1</a>', digest)
        self.assertIn('<a href="https://github.com/owner/braindump/issues/3">braindump#3</a>', digest)
        self.assertTrue(digest.endswith("Which one do you want to start first?"))
        (req,) = self.http.sent("anthropic")
        self.assertEqual(req["model"], "test-model")
        self.assertIn("repo-a#1 (bug, asap): Fix login <crash>", req["messages"][0]["content"])

    def test_llm_output_is_html_escaped_before_linking(self):
        self.http.anthropic = lambda p, b: llm_response("repo-a#1 <b>bold?</b> & done")
        digest = D.build_digest(D.collect_issues())
        self.assertIn("&lt;b&gt;bold?&lt;/b&gt; &amp; done", digest)
        self.assertIn('<a href="https://github.com/owner/repo-a/issues/1">repo-a#1</a>', digest)

    def test_llm_failure_falls_back_to_raw_list(self):
        self.http.fail["anthropic"] = 503
        digest = D.build_digest(D.collect_issues())
        self.assertIn("[repo-a]", digest)
        self.assertIn('<a href="https://github.com/owner/repo-a/issues/1">repo-a#1</a>', digest)
        self.assertTrue(digest.endswith("Which one do you want to start first?"))

    def test_empty_backlog_skips_llm(self):
        digest = D.build_digest([])
        self.assertIn("Nothing open", digest)
        self.assertEqual(self.http.sent("anthropic"), [])

    def test_chunking_never_splits_a_line(self):
        lines = [f"line {i} " + "x" * 90 for i in range(100)]
        chunks = D.chunk("\n".join(lines), limit=1000)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 1000)
        self.assertEqual("\n".join(chunks).split("\n"), lines)


class Delivery(DigestTestCase):
    def test_scheduled_run_sends_to_owner(self):
        result = D.lambda_handler({"source": "schedule"}, None)
        self.assertEqual(result["issues"], 3)
        (msg,) = self.http.sent("telegram", "sendMessage")
        self.assertEqual(msg["chat_id"], 42)  # ALLOWED_USER_ID
        self.assertEqual(msg["parse_mode"], "HTML")
        self.assertTrue(msg["disable_web_page_preview"])
        self.assertEqual(self.http.sent("telegram", "editMessageText"), [])

    def test_on_demand_run_edits_placeholder(self):
        D.lambda_handler({"chat_id": 42, "message_id": 555}, None)
        (edit,) = self.http.sent("telegram", "editMessageText")
        self.assertEqual(edit["message_id"], 555)
        self.assertIn("<b>3 open issues</b>", edit["text"])
        self.assertEqual(self.http.sent("telegram", "sendMessage"), [])

    def test_long_digest_spills_into_follow_up_messages(self):
        self.http.anthropic = lambda p, b: llm_response("\n".join(f"line {i} " + "y" * 100 for i in range(60)))
        D.lambda_handler({"chat_id": 42, "message_id": 555}, None)
        self.assertEqual(len(self.http.sent("telegram", "editMessageText")), 1)
        self.assertGreaterEqual(len(self.http.sent("telegram", "sendMessage")), 1)
        for m in self.http.sent("telegram"):
            self.assertLessEqual(len(m["text"]), D.TELEGRAM_TEXT_LIMIT)

    def test_github_failure_is_reported_then_raised(self):
        self.http.fail["github"] = 500
        with self.assertRaises(RuntimeError):
            D.lambda_handler({"chat_id": 42, "message_id": 555}, None)
        (edit,) = self.http.sent("telegram", "editMessageText")
        self.assertIn("Couldn't build the digest", edit["text"])

    def test_401_from_github_evicts_cached_pat(self):
        D._cache["/anyissues-test/github-pat"] = "stale"

        def unauthorised(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(b"{}"))

        with mock.patch("urllib.request.urlopen", unauthorised), self.assertRaises(RuntimeError):
            D.fetch_tracked_repos()
        self.assertNotIn("/anyissues-test/github-pat", D._cache)


class WebhookDigestCommand(unittest.TestCase):
    def setUp(self):
        self.W = load_handler("webhook_handler")
        reset_module(self.W)
        self.http = FakeHTTP()
        self._patch = self.http.patch()
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.W.drafts_table.scan.return_value = {"Items": [{"draft_id": "old00000", "status": "awaiting_edit"}]}

    def send(self, text):
        from _harness import message_update, webhook_event

        return self.W.lambda_handler(webhook_event(message_update(9, text)), None)

    def test_digest_command_posts_placeholder_and_invokes_digest_lambda(self):
        for text in ("/digest", "/DIGEST now", "/digest@anyissue_bot"):
            with self.subTest(text=text):
                self.W.lambda_client.reset_mock()
                self.send(text)
                kw = self.W.lambda_client.invoke.call_args.kwargs
                self.assertEqual(kw["FunctionName"], "anyissue-digest")
                self.assertEqual(kw["InvocationType"], "Event")
                self.assertEqual(json.loads(kw["Payload"]), {"chat_id": 42, "message_id": 777})
        self.assertIn("building digest", self.http.sent("telegram", "sendMessage")[0]["text"])
        # Even with an edit pending, /digest is a command, not a correction.
        self.W.drafts_table.update_item.assert_not_called()
        self.W.sqs.send_message.assert_not_called()

    def test_ordinary_text_does_not_invoke_digest(self):
        self.W.drafts_table.scan.return_value = {"Items": []}
        self.send("digest the logs please")
        self.W.lambda_client.invoke.assert_not_called()
        self.W.sqs.send_message.assert_called_once()


class WorkloadIdentityFederation(DigestTestCase):
    def run_digest(self):
        return D.lambda_handler({"source": "schedule"}, None)

    def test_default_state_uses_the_ssm_key_not_wif(self):
        self.run_digest()
        D.sts.get_web_identity_token.assert_not_called()
        (headers,) = self.http.headers_sent("anthropic", "/messages")
        self.assertIn("x-api-key", headers)

    def test_wif_used_when_configured(self):
        with wif_enabled(D):
            self.run_digest()
        D.sts.get_web_identity_token.assert_called_once_with(
            Audience=["https://api.anthropic.com"], SigningAlgorithm="RS256", DurationSeconds=900
        )
        (oauth_req,) = self.http.sent("anthropic", "/oauth/token")
        self.assertEqual(oauth_req["federation_rule_id"], "fdrl_test")
        (headers,) = self.http.headers_sent("anthropic", "/messages")
        self.assertEqual(headers["authorization"], "Bearer sk-ant-oat01-test")
        self.assertNotIn("x-api-key", headers)

    def test_oauth_exchange_failure_falls_back_and_digest_still_sends(self):
        self.http.oauth_fail = 401
        with wif_enabled(D):
            self.run_digest()
        (headers,) = self.http.headers_sent("anthropic", "/messages")
        self.assertIn("x-api-key", headers)
        self.assertIn("<b>3 open issues</b>", self.http.sent("telegram", "sendMessage")[0]["text"])

    def test_missing_boto3_method_falls_back_to_ssm_key(self):
        D.sts.get_web_identity_token.side_effect = AttributeError("no such method")
        with wif_enabled(D):
            self.run_digest()
        self.assertEqual(self.http.sent("anthropic", "/oauth/token"), [])
        (headers,) = self.http.headers_sent("anthropic", "/messages")
        self.assertIn("x-api-key", headers)

    def test_token_reused_across_repeated_invocations(self):
        with wif_enabled(D):
            self.run_digest()
            self.run_digest()
        self.assertEqual(D.sts.get_web_identity_token.call_count, 1)


if __name__ == "__main__":
    unittest.main()
