"""
Consumes one SQS message per draft. Turns raw brain-dump text into a
{title, body} pair via the Anthropic API, fetches the current list of
tracked repos from GitHub (topic search), writes both onto the draft, and
edits the Telegram placeholder message into the real preview with buttons.
"""
import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

import boto3

DRAFTS_TABLE = os.environ["DRAFTS_TABLE"]
DRAFT_TTL_HOURS = int(os.environ["DRAFT_TTL_HOURS"])
GITHUB_OWNER = os.environ["GITHUB_OWNER"]
BRAINDUMP_REPO = os.environ["BRAINDUMP_REPO"]
ANTHROPIC_MODEL = os.environ["ANTHROPIC_MODEL"]
TELEGRAM_BOT_TOKEN_PARAM = os.environ["TELEGRAM_BOT_TOKEN_PARAM"]
ANTHROPIC_API_KEY_PARAM = os.environ["ANTHROPIC_API_KEY_PARAM"]
GITHUB_PAT_PARAM = os.environ["GITHUB_PAT_PARAM"]

dynamodb = boto3.resource("dynamodb")
drafts_table = dynamodb.Table(DRAFTS_TABLE)
ssm = boto3.client("ssm")

# Secrets are cached for the life of the Lambda container (minutes to a few
# hours). Tradeoff, deliberately accepted: after rotating a parameter in SSM,
# warm containers keep the old value until they are recycled or the
# function is redeployed. The safety valve below handles the common case --
# a 401/403 from the upstream API evicts the cached value so the next call
# re-reads SSM Parameter Store -- without paying a GetParameter call on every request.
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
    # boto3's resource layer returns every DynamoDB number as Decimal, which
    # json.dumps refuses. chat_id / message_id must round-trip as ints.
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def _telegram(method, payload):
    token = _param(TELEGRAM_BOT_TOKEN_PARAM)
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(
        url,
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
    """Escape for Telegram parse_mode=HTML. Only < > & are special there, so
    LLM output full of _ * [ ] renders verbatim instead of breaking the call."""
    return html.escape(str(text), quote=False)


SYSTEM_PROMPT = (
    "You turn a rough, unstructured brain-dump into an actionable engineering "
    "ticket. Respond with ONLY a JSON object, no prose, no markdown fences, "
    'shaped exactly as: {"title": "...", "body": "..."}. '
    "title: <=70 chars, imperative, specific. "
    "body: markdown with a one-line problem statement, then a short bulleted "
    "'Acceptance criteria' list. Do not invent facts not implied by the input."
)

# Structured outputs (output_config.format) make the API itself guarantee the
# response is a JSON object matching this schema, so the parsing below is a
# belt-and-braces path rather than the primary mechanism.
TICKET_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["title", "body"],
    "additionalProperties": False,
}


class ShapeError(Exception):
    """LLM shaping failed for a reason that will not go away on retry."""


class TransientError(Exception):
    """Upstream hiccup (429/5xx/timeout): let SQS redeliver, then DLQ."""


def _extract_ticket(text):
    """Turn whatever the model wrote into (title, body) or raise ShapeError."""
    text = text.strip()
    # Tolerate fenced or prose-wrapped JSON by slicing to the outermost braces.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ShapeError(f"no JSON object in model output: {text[:120]!r}")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise ShapeError(f"model output is not valid JSON: {e}") from None
    if not isinstance(parsed, dict):
        raise ShapeError("model output is not a JSON object")
    title, body = parsed.get("title"), parsed.get("body")
    if not isinstance(title, str) or not title.strip():
        raise ShapeError("model output missing 'title'")
    if not isinstance(body, str):
        raise ShapeError("model output missing 'body'")
    return title.strip()[:200], body.strip()


def shape_with_llm(raw_text):
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(
            {
                "model": ANTHROPIC_MODEL,
                "max_tokens": 2000,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": raw_text}],
                "output_config": {"format": {"type": "json_schema", "schema": TICKET_SCHEMA}},
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
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code in (401, 403):
            _wif.pop("access_token", None)
            _forget_param(ANTHROPIC_API_KEY_PARAM)
        if e.code == 429 or e.code >= 500:
            raise TransientError(f"Anthropic {e.code}: {detail}") from None
        raise ShapeError(f"Anthropic {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise TransientError(f"Anthropic unreachable: {e}") from None

    stop = data.get("stop_reason")
    if stop == "refusal":
        raise ShapeError("model refused the request")
    if stop == "max_tokens":
        raise ShapeError("model output truncated at max_tokens")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return _extract_ticket(text)


def fetch_tracked_repos():
    pat = _param(GITHUB_PAT_PARAM)
    # Every repo you own that isn't a fork or archived is a project. The
    # braindump repo is excluded from the buttons: "eureka" already routes there.
    query = urllib.parse.quote(f"user:{GITHUB_OWNER} archived:false fork:false")
    req = urllib.request.Request(
        f"https://api.github.com/search/repositories?q={query}&sort=updated&per_page=100",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        return [item["name"] for item in data.get("items", []) if item["name"] != BRAINDUMP_REPO]
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _forget_param(GITHUB_PAT_PARAM)
        print("repo search failed:", e.code, e.read().decode(errors="replace"))
        return []


ICONS = {"bug": "\U0001F41B", "feature": "\u2728", "eureka": "\U0001F9E0"}
URGENCY_ICONS = {"asap": "\U0001F525", "normal": "\u23FA", "good-to-have": "\U0001F4A4"}


def build_keyboard(draft):
    rows = []
    repos = draft.get("repos_snapshot", [])
    options = repos + ["eureka"]
    project_row = []
    for i, name in enumerate(options):
        selected = draft.get("project") == name
        label = f"eureka {ICONS['eureka']}" if name == "eureka" else name
        mark = "\u2705 " if selected else ""
        project_row.append({"text": f"{mark}{label}", "callback_data": f"d:{draft['draft_id']}:p:{i}"})
    for i in range(0, len(project_row), 2):
        rows.append(project_row[i:i + 2])

    if draft.get("project") != "eureka":
        type_row = []
        for t in ("bug", "feature"):
            mark = "\u2705 " if draft.get("type") == t else ""
            type_row.append({"text": f"{mark}{ICONS[t]} {t}", "callback_data": f"d:{draft['draft_id']}:t:{t}"})
        rows.append(type_row)

    urgency_row = []
    for u in ("asap", "normal", "good-to-have"):
        mark = "\u2705 " if draft.get("urgency") == u else ""
        urgency_row.append({
            "text": f"{mark}{URGENCY_ICONS[u]} {u}",
            "callback_data": f"d:{draft['draft_id']}:u:{u}",
        })
    rows.append(urgency_row)

    rows.append([
        {"text": "\u2705 Confirm", "callback_data": f"d:{draft['draft_id']}:c"},
        {"text": "\u270F\uFE0F Edit", "callback_data": f"d:{draft['draft_id']}:e"},
        {"text": "\U0001F5D1 Discard", "callback_data": f"d:{draft['draft_id']}:x"},
    ])
    return {"inline_keyboard": rows}


TELEGRAM_TEXT_LIMIT = 4096


def preview_text(title, body, raw_text):
    text = f"<b>{_h(title)}</b>\n\n{_h(body)}\n\n<i>raw: {_h(raw_text[:200])}</i>"
    if len(text) > TELEGRAM_TEXT_LIMIT:
        # Trim the body, never the tags: cutting inside <b>/<i> would break parsing.
        budget = TELEGRAM_TEXT_LIMIT - len(text) + len(_h(body)) - 1
        text = f"<b>{_h(title)}</b>\n\n{_h(body)[: max(budget, 0)]}\u2026\n\n<i>raw: {_h(raw_text[:200])}</i>"
    return text


def process_one(draft_id):
    resp = drafts_table.get_item(Key={"draft_id": draft_id})
    draft = resp.get("Item")
    if not draft:
        print(f"draft {draft_id} not found, skipping")
        return

    try:
        title, body = shape_with_llm(draft["raw_text"])
    except TransientError:
        # Re-raise so SQS retries (maxReceiveCount 3) and then parks it in the DLQ.
        raise
    except Exception as e:  # noqa: BLE001 - anything else is a permanent failure for this draft
        print(f"LLM shaping failed for {draft_id}: {type(e).__name__}: {e}")
        drafts_table.update_item(
            Key={"draft_id": draft_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "failed"},
        )
        _telegram("editMessageText", {
            "chat_id": draft["chat_id"],
            "message_id": draft["message_id"],
            "text": "\u26A0\uFE0F Couldn't shape that one -- send it again, or check the logs.",
        })
        return

    repos = fetch_tracked_repos()

    now = int(time.time())
    drafts_table.update_item(
        Key={"draft_id": draft_id},
        # "ttl", "type", "status" and "project" are DynamoDB reserved words -> aliased.
        UpdateExpression=(
            "SET llm_title = :t, llm_body = :b, repos_snapshot = :r, "
            "#s = :s, #ttl = :ttl REMOVE #p, #ty, urgency"
        ),
        ExpressionAttributeNames={"#s": "status", "#ty": "type", "#ttl": "ttl", "#p": "project"},
        ExpressionAttributeValues={
            ":t": title, ":b": body, ":r": repos, ":s": "pending",
            ":ttl": now + DRAFT_TTL_HOURS * 3600,
        },
    )

    draft["draft_id"] = draft_id
    draft["repos_snapshot"] = repos
    draft["project"] = None
    draft["type"] = None
    draft["urgency"] = None

    _telegram("editMessageText", {
        "chat_id": draft["chat_id"],
        "message_id": draft["message_id"],
        "text": preview_text(title, body, draft["raw_text"]),
        "parse_mode": "HTML",
        "reply_markup": build_keyboard(draft),
    })


def lambda_handler(event, context):
    for record in event["Records"]:
        body = json.loads(record["body"])
        process_one(body["draft_id"])
    return {"statusCode": 200}
