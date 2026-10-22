"""
Outstanding-work digest.

Reads every open issue across your non-fork, non-archived repos (the same
GitHub search the capture flow uses for its buttons) plus the braindump repo, asks the Anthropic API for a short
prioritised summary, and sends it to the owner on Telegram with the question
"which one do you want to start first?".

Two entry points, one handler:
- EventBridge Scheduler (daily cron)  -> event is {} / {"source": "schedule"};
                                         sends a fresh message to ALLOWED_USER_ID.
- /digest command on Telegram         -> webhook_handler async-invokes this
                                         function with {"chat_id", "message_id"};
                                         the summary replaces that placeholder.

Kept dependency-free (stdlib only: urllib, json, boto3) so `terraform apply`
never needs a separate pip-install/build step.
"""

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

import boto3

GITHUB_OWNER = os.environ["GITHUB_OWNER"]
BRAINDUMP_REPO = os.environ["BRAINDUMP_REPO"]
ANTHROPIC_MODEL = os.environ["ANTHROPIC_MODEL"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
TELEGRAM_BOT_TOKEN_PARAM = os.environ["TELEGRAM_BOT_TOKEN_PARAM"]
ANTHROPIC_API_KEY_PARAM = os.environ["ANTHROPIC_API_KEY_PARAM"]
GITHUB_PAT_PARAM = os.environ["GITHUB_PAT_PARAM"]

ssm = boto3.client("ssm")

# Same caching tradeoff as the other handlers: values live for the container's
# lifetime; a 401/403 upstream evicts them so rotation heals on the next call.
_cache = {}


def _param(path):
    if path not in _cache:
        _cache[path] = ssm.get_parameter(Name=path, WithDecryption=True)["Parameter"]["Value"]
    return _cache[path]


def _forget_param(path):
    _cache.pop(path, None)


# ---------------------------------------------------------------- Anthropic auth
#
# Preferred: Workload Identity Federation. sts:GetWebIdentityToken mints an
# AWS-signed JWT asserting this Lambda's own role ARN; the federation rule
# (Claude Console -> Settings -> Workload identity, matched on that ARN)
# exchanges it at /v1/oauth/token for a short-lived Anthropic access token.
# No static Anthropic key anywhere in this path.
#
# Fallback: the SSM-stored API key. Two reasons this isn't trusted blindly
# yet -- (1) GetWebIdentityToken is a very new STS API and the Lambda-bundled
# boto3 may lag it, which surfaces as AttributeError; (2) belt-and-braces
# during the initial rollout. Once CloudWatch confirms WIF is the path
# actually being used, drop this fallback and ANTHROPIC_API_KEY_PARAM
# (env var, IAM grant, and the SSM parameter itself) entirely.
ANTHROPIC_FEDERATION_RULE_ID = os.environ.get("ANTHROPIC_FEDERATION_RULE_ID")
ANTHROPIC_ORGANIZATION_ID = os.environ.get("ANTHROPIC_ORGANIZATION_ID")
ANTHROPIC_SERVICE_ACCOUNT_ID = os.environ.get("ANTHROPIC_SERVICE_ACCOUNT_ID")
# Optional: only needed if the federation rule spans more than one workspace.
ANTHROPIC_WORKSPACE_ID = os.environ.get("ANTHROPIC_WORKSPACE_ID")

sts = boto3.client("sts")

_wif = {}  # {"access_token": str, "expires_at": epoch seconds, "logged": bool}


def _wif_configured():
    return bool(ANTHROPIC_FEDERATION_RULE_ID and ANTHROPIC_ORGANIZATION_ID and ANTHROPIC_SERVICE_ACCOUNT_ID)


def _exchange_wif_token():
    web_identity_token = sts.get_web_identity_token(
        Audience=["https://api.anthropic.com"],
        SigningAlgorithm="RS256",
        DurationSeconds=900,
    )["WebIdentityToken"]
    body = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": web_identity_token,
        "federation_rule_id": ANTHROPIC_FEDERATION_RULE_ID,
        "organization_id": ANTHROPIC_ORGANIZATION_ID,
        "service_account_id": ANTHROPIC_SERVICE_ACCOUNT_ID,
    }
    if ANTHROPIC_WORKSPACE_ID:
        body["workspace_id"] = ANTHROPIC_WORKSPACE_ID
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/oauth/token",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"oauth/token -> {e.code}: {detail}") from None


def _wif_access_token():
    now = time.time()
    if _wif.get("access_token") and _wif.get("expires_at", 0) > now + 60:
        return _wif["access_token"]
    data = _exchange_wif_token()
    _wif["access_token"] = data["access_token"]
    _wif["expires_at"] = now + data.get("expires_in", 3600)
    return _wif["access_token"]


def _anthropic_auth_header():
    """Auth header for the Messages API: prefer WIF, fall back to the
    SSM-stored key. Logs which path is live, once per container."""
    if _wif_configured():
        try:
            token = _wif_access_token()
            if not _wif.get("logged"):
                print("anthropic auth: workload identity federation")
                _wif["logged"] = True
            return {"Authorization": f"Bearer {token}"}
        except AttributeError:
            print("anthropic auth: boto3 lacks sts.get_web_identity_token -- falling back to SSM key")
        except Exception as e:  # noqa: BLE001 - any WIF failure falls back rather than breaking shaping
            print(f"anthropic auth: WIF exchange failed ({type(e).__name__}: {e}) -- falling back to SSM key")
            _wif.pop("access_token", None)
    return {"x-api-key": _param(ANTHROPIC_API_KEY_PARAM)}


def _json_default(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def _telegram(method, payload):
    token = _param(TELEGRAM_BOT_TOKEN_PARAM)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload, default=_json_default).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _forget_param(TELEGRAM_BOT_TOKEN_PARAM)
        raise RuntimeError(f"Telegram {method} -> {e.code}: {e.read().decode(errors='replace')}") from None


def _h(text):
    return html.escape(str(text), quote=False)


# ---------------------------------------------------------------- GitHub


def _github_get(path):
    """GET api.github.com{path}; returns (json, Link header or '')."""
    pat = _param(GITHUB_PAT_PARAM)
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()), resp.headers.get("Link", "")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _forget_param(GITHUB_PAT_PARAM)
        raise RuntimeError(f"GitHub GET {path} -> {e.code}: {e.read().decode(errors='replace')}") from None


def fetch_tracked_repos():
    query = urllib.parse.quote(f"user:{GITHUB_OWNER} archived:false fork:false")
    data, _ = _github_get(f"/search/repositories?q={query}&sort=updated&per_page=100")
    names = [item["name"] for item in data.get("items", [])]
    if BRAINDUMP_REPO not in names:
        names.append(BRAINDUMP_REPO)
    return names


MAX_PAGES_PER_REPO = 5  # 500 open issues per repo is far beyond a personal backlog


def fetch_open_issues(repo):
    """All open issues in owner/repo as compact dicts. Pull requests are
    excluded: GitHub's issues endpoint returns those too."""
    issues = []
    path = f"/repos/{GITHUB_OWNER}/{repo}/issues?state=open&per_page=100"
    for _ in range(MAX_PAGES_PER_REPO):
        page, link = _github_get(path)
        for item in page:
            if "pull_request" in item:
                continue
            issues.append(
                {
                    "repo": repo,
                    "number": item["number"],
                    "title": item["title"],
                    "labels": [label["name"] for label in item.get("labels", [])],
                    "url": item["html_url"],
                    "updated_at": item.get("updated_at", "")[:10],
                }
            )
        match = re.search(r'<https://api\.github\.com([^>]+)>;\s*rel="next"', link)
        if not match:
            break
        path = match.group(1)
    return issues


def collect_issues():
    issues = []
    for repo in fetch_tracked_repos():
        issues.extend(fetch_open_issues(repo))
    return issues


# ---------------------------------------------------------------- summary

URGENCY_ORDER = ("urgency:asap", "urgency:normal", "urgency:good-to-have")
URGENCY_BUCKETS = ("asap", "normal", "good-to-have", "unlabelled")


def urgency_of(issue):
    for label in URGENCY_ORDER:
        if label in issue["labels"]:
            return label.removeprefix("urgency:")
    return "unlabelled"


def issue_ref(issue):
    return f"{issue['repo']}#{issue['number']}"


def ordered_repos(issues):
    """Repos sorted by most urgency:asap issues first, ties broken by Normal
    count then Good-to-have count. BRAINDUMP_REPO always sorts last, whatever
    its own counts are."""
    counts = {}
    for i in issues:
        counts.setdefault(i["repo"], {b: 0 for b in URGENCY_BUCKETS})[urgency_of(i)] += 1

    def key(repo):
        c = counts[repo]
        return (repo == BRAINDUMP_REPO, -c["asap"], -c["normal"], -c["good-to-have"], repo)

    return sorted(counts, key=key)


def issues_as_text(issues):
    """The deterministic input the model sees, and the fallback if it fails.
    Grouped by repository (see ordered_repos), urgency-first within each repo."""
    by_repo = {}
    for i in issues:
        by_repo.setdefault(i["repo"], []).append(i)
    lines = []
    for repo in ordered_repos(issues):
        lines.append(f"[{repo}]")
        for bucket in URGENCY_BUCKETS:
            for i in (x for x in by_repo[repo] if urgency_of(x) == bucket):
                kind = next((label for label in i["labels"] if label in ("bug", "feature", "eureka")), "")
                tags = ", ".join(filter(None, [kind, bucket]))
                lines.append(f"- {issue_ref(i)} ({tags}): {i['title']}  (updated {i['updated_at']})")
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You are summarising a solo developer's outstanding GitHub issues so they can "
    "pick what to work on next. Write PLAIN TEXT for a chat message: no markdown, "
    "no headings with #, no bold markers. Group by repository, not by urgency -- "
    "the repo with the most urgency:asap issues first, ties broken by Normal count "
    f"then Good-to-have count, and always put {BRAINDUMP_REPO} last regardless of "
    "its own counts. Within each repo, list issues urgency-first (asap, then "
    "normal, then good-to-have). One short line per issue, and refer to every "
    "issue by its exact repo#number token so it can be linked. Call out anything "
    "stale or clearly related. Stay under 2000 characters. Finish with exactly "
    "this line: Which one do you want to start first?"
)


def summarise_with_llm(issues):
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(
            {
                "model": ANTHROPIC_MODEL,
                "max_tokens": 2000,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": issues_as_text(issues)}],
            }
        ).encode(),
        headers={
            **_anthropic_auth_header(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _wif.pop("access_token", None)
            _forget_param(ANTHROPIC_API_KEY_PARAM)
        raise RuntimeError(f"Anthropic -> {e.code}: {e.read().decode(errors='replace')[:300]}") from None
    if data.get("stop_reason") == "refusal":
        raise RuntimeError("model refused")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if not text:
        raise RuntimeError("empty model response")
    return text


def linkify(text, issues):
    """HTML-escape, then turn every known repo#number token into a link."""
    escaped = _h(text)
    by_ref = {issue_ref(i): i["url"] for i in issues}
    if not by_ref:
        return escaped
    pattern = re.compile(r"\b(" + "|".join(re.escape(ref) for ref in sorted(by_ref, key=len, reverse=True)) + r")\b")
    return pattern.sub(lambda m: f'<a href="{by_ref[m.group(1)]}">{m.group(1)}</a>', escaped)


def build_digest(issues):
    repos = sorted({i["repo"] for i in issues})
    if not issues:
        return "\U0001f389 Nothing open across your tracked repos. Enjoy the quiet."
    n_issues = f"{len(issues)} open issue{'s' if len(issues) != 1 else ''}"
    n_repos = f"{len(repos)} repo{'s' if len(repos) != 1 else ''}"
    header = f"\U0001f4cb <b>{n_issues}</b> across {n_repos}\n\n"
    try:
        body = summarise_with_llm(issues)
    except Exception as e:  # noqa: BLE001 - the digest must still go out
        print(f"LLM summary failed, sending raw list: {type(e).__name__}: {e}")
        body = issues_as_text(issues) + "\n\nWhich one do you want to start first?"
    return header + linkify(body, issues)


# ---------------------------------------------------------------- delivery

TELEGRAM_TEXT_LIMIT = 4096


def chunk(text, limit=TELEGRAM_TEXT_LIMIT):
    """Split on line boundaries so an <a> tag is never cut in half."""
    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


def deliver(chat_id, text, message_id=None):
    first, *rest = chunk(text)
    if message_id is not None:
        _telegram(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": first,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
    else:
        _telegram(
            "sendMessage", {"chat_id": chat_id, "text": first, "parse_mode": "HTML", "disable_web_page_preview": True}
        )
    for part in rest:
        _telegram(
            "sendMessage", {"chat_id": chat_id, "text": part, "parse_mode": "HTML", "disable_web_page_preview": True}
        )


def lambda_handler(event, context):
    event = event or {}
    chat_id = event.get("chat_id", ALLOWED_USER_ID)
    message_id = event.get("message_id")
    try:
        issues = collect_issues()
    except Exception as e:  # noqa: BLE001 - tell the user rather than dying silently
        print(f"digest failed: {type(e).__name__}: {e}")
        deliver(chat_id, "⚠️ Couldn't build the digest -- GitHub call failed, check the logs.", message_id)
        raise
    deliver(chat_id, build_digest(issues), message_id)
    return {"statusCode": 200, "issues": len(issues)}
