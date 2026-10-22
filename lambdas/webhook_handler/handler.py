"""
Single entry point for the Telegram webhook.

- New text message              -> write a "processing" draft, ping the user
                                    with a placeholder, enqueue to SQS, return.
- New text message while a      -> treat as a correction: re-enqueue against
  draft is awaiting_edit            the SAME draft_id instead of making a new one.
- callback_query (button tap)   -> handled entirely here, synchronously:
                                    mutate the draft, re-render the keyboard,
                                    and on Confirm, create the GitHub issue.
- /digest command               -> post a placeholder and async-invoke the
                                    digest Lambda, which edits the summary in.

Kept dependency-free (stdlib only: urllib, json, boto3) so `terraform apply`
never needs a separate pip-install/build step.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

DRAFTS_TABLE = os.environ["DRAFTS_TABLE"]
SHAPE_QUEUE_URL = os.environ["SHAPE_QUEUE_URL"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
DRAFT_TTL_HOURS = int(os.environ["DRAFT_TTL_HOURS"])
BRAINDUMP_REPO = os.environ["BRAINDUMP_REPO"]
GITHUB_OWNER = os.environ["GITHUB_OWNER"]
TELEGRAM_BOT_TOKEN_PARAM = os.environ["TELEGRAM_BOT_TOKEN_PARAM"]
GITHUB_PAT_PARAM = os.environ["GITHUB_PAT_PARAM"]
DIGEST_FUNCTION_NAME = os.environ["DIGEST_FUNCTION_NAME"]

dynamodb = boto3.resource("dynamodb")
drafts_table = dynamodb.Table(DRAFTS_TABLE)
sqs = boto3.client("sqs")
ssm = boto3.client("ssm")
lambda_client = boto3.client("lambda")

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
        # Telegram puts the reason in the body ("Bad Request: message is not
        # modified", "can't parse entities", ...). Surface it instead of a bare 400.
        raise RuntimeError(f"Telegram {method} -> {e.code}: {e.read().decode(errors='replace')}") from None


def _github(method, path, body=None):
    pat = _param(GITHUB_PAT_PARAM)
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(body, default=_json_default).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _forget_param(GITHUB_PAT_PARAM)
        raise RuntimeError(f"GitHub {method} {path} -> {e.code}: {e.read().decode(errors='replace')}") from None


ICONS = {"bug": "\U0001F41B", "feature": "\u2728", "eureka": "\U0001F9E0"}
URGENCY_ICONS = {"asap": "\U0001F525", "normal": "\u23FA", "good-to-have": "\U0001F4A4"}
URGENCY_LABEL = {"asap": "ASAP", "normal": "normal", "good-to-have": "good-to-have"}


def build_keyboard(draft):
    rows = []

    project_row = []
    repos = draft.get("repos_snapshot", [])
    options = repos + ["eureka"]
    for i, name in enumerate(options):
        selected = draft.get("project") == name
        label = f"{'eureka ' + ICONS['eureka'] if name == 'eureka' else name}"
        mark = "\u2705 " if selected else ""
        project_row.append({"text": f"{mark}{label}", "callback_data": f"d:{draft['draft_id']}:p:{i}"})
    # chunk into rows of 2
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
            "text": f"{mark}{URGENCY_ICONS[u]} {URGENCY_LABEL[u]}",
            "callback_data": f"d:{draft['draft_id']}:u:{u}",
        })
    rows.append(urgency_row)

    rows.append([
        {"text": "\u2705 Confirm", "callback_data": f"d:{draft['draft_id']}:c"},
        {"text": "\u270F\uFE0F Edit", "callback_data": f"d:{draft['draft_id']}:e"},
        {"text": "\U0001F5D1 Discard", "callback_data": f"d:{draft['draft_id']}:x"},
    ])
    return {"inline_keyboard": rows}


def enqueue_shape(draft_id, is_edit=False):
    sqs.send_message(
        QueueUrl=SHAPE_QUEUE_URL,
        MessageBody=json.dumps({"draft_id": draft_id, "is_edit": is_edit}),
    )


def handle_new_message(chat_id, text):
    draft_id = uuid.uuid4().hex[:8]
    placeholder = _telegram("sendMessage", {"chat_id": chat_id, "text": "\U0001F9E0 thinking..."})
    message_id = placeholder["result"]["message_id"]

    now = int(time.time())
    drafts_table.put_item(Item={
        "draft_id": draft_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "status": "processing",
        "raw_text": text,
        "created_at": now,
        "ttl": now + DRAFT_TTL_HOURS * 3600,
    })
    enqueue_shape(draft_id)


def handle_edit_message(draft, text):
    drafts_table.update_item(
        Key={"draft_id": draft["draft_id"]},
        # "ttl" is a DynamoDB reserved word: it must be aliased or the call is rejected.
        UpdateExpression="SET raw_text = :t, #s = :s, #ttl = :ttl",
        ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
        ExpressionAttributeValues={
            ":t": text,
            ":s": "processing",
            ":ttl": int(time.time()) + DRAFT_TTL_HOURS * 3600,
        },
    )
    _telegram("editMessageText", {
        "chat_id": draft["chat_id"],
        "message_id": draft["message_id"],
        "text": "\U0001F9E0 thinking...",
    })
    enqueue_shape(draft["draft_id"], is_edit=True)


def handle_digest_command(chat_id):
    """The digest takes several GitHub calls plus an LLM round-trip -- far too
    slow for a webhook response -- so it runs in its own Lambda, invoked
    asynchronously, and edits its result into the placeholder posted here."""
    placeholder = _telegram("sendMessage", {"chat_id": chat_id, "text": "\U0001f4cb building digest..."})
    lambda_client.invoke(
        FunctionName=DIGEST_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps({"chat_id": chat_id, "message_id": placeholder["result"]["message_id"]}).encode(),
    )


def find_awaiting_edit_draft(chat_id):
    # Deliberately a Scan, not a GSI. This table only ever holds the current
    # user's live drafts (TTL evicts the rest within DRAFT_TTL_HOURS), so a
    # scan touches a handful of items and costs well under one RCU per message.
    # A GSI on (chat_id, status) would add index storage, write amplification on
    # every status change, and eventual-consistency edge cases in the edit flow,
    # for no measurable gain at single-user volume. Revisit only if this ever
    # becomes multi-user.
    resp = drafts_table.scan(
        FilterExpression="chat_id = :c AND #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":c": chat_id, ":s": "awaiting_edit"},
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def answer_callback(callback_query_id, text=None, alert=False):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = alert
    _telegram("answerCallbackQuery", payload)


def create_issue(draft):
    if draft.get("project") == "eureka":
        repo = BRAINDUMP_REPO
    else:
        repo = draft["project"]

    labels = [draft["type"], f"urgency:{draft['urgency']}"]
    body = (
        f"{draft.get('llm_body', '')}\n\n"
        f"---\n*Raw input:*\n> {draft.get('raw_text', '')}\n\n"
        f"_Filed via AnyIssues._"
    )
    result = _github("POST", f"/repos/{GITHUB_OWNER}/{repo}/issues", {
        "title": draft.get("llm_title", draft.get("raw_text", "")[:80]),
        "body": body,
        "labels": labels,
    })
    return result["html_url"]


def handle_callback(callback_query):
    data = callback_query["data"]  # "d:<id>:<field>[:<value>]"
    parts = data.split(":")
    draft_id = parts[1]
    field = parts[2]
    value = parts[3] if len(parts) > 3 else None
    callback_id = callback_query["id"]

    resp = drafts_table.get_item(Key={"draft_id": draft_id})
    draft = resp.get("Item")
    if not draft:
        answer_callback(callback_id, "This draft expired.", alert=True)
        return

    if field == "p":
        repos = draft.get("repos_snapshot", [])
        options = repos + ["eureka"]
        chosen = options[int(value)]
        update = {"project": chosen}
        if chosen == "eureka":
            update["type"] = "eureka"
        # "type" is a DynamoDB reserved word, so every attribute goes through an alias.
        drafts_table.update_item(
            Key={"draft_id": draft_id},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in update),
            ExpressionAttributeNames={f"#{k}": k for k in update},
            ExpressionAttributeValues={f":{k}": v for k, v in update.items()},
        )
        draft.update(update)
        answer_callback(callback_id)

    elif field == "t":
        drafts_table.update_item(
            Key={"draft_id": draft_id},
            UpdateExpression="SET #t = :v",
            ExpressionAttributeNames={"#t": "type"},
            ExpressionAttributeValues={":v": value},
        )
        draft["type"] = value
        answer_callback(callback_id)

    elif field == "u":
        drafts_table.update_item(
            Key={"draft_id": draft_id},
            UpdateExpression="SET urgency = :v",
            ExpressionAttributeValues={":v": value},
        )
        draft["urgency"] = value
        answer_callback(callback_id)

    elif field == "x":
        drafts_table.update_item(
            Key={"draft_id": draft_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "discarded"},
        )
        _telegram("editMessageText", {
            "chat_id": draft["chat_id"], "message_id": draft["message_id"],
            "text": "\u274C Discarded.",
        })
        answer_callback(callback_id)
        return

    elif field == "e":
        drafts_table.update_item(
            Key={"draft_id": draft_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "awaiting_edit"},
        )
        _telegram("editMessageText", {
            "chat_id": draft["chat_id"], "message_id": draft["message_id"],
            "text": "\u270F\uFE0F Send your corrected description as the next message.",
        })
        answer_callback(callback_id)
        return

    elif field == "c":
        if draft.get("status") == "confirmed":
            answer_callback(callback_id, "Already filed.")
            return
        missing = [f for f in ("project", "urgency") if not draft.get(f)]
        if draft.get("project") != "eureka" and not draft.get("type"):
            missing.append("type")
        if missing:
            answer_callback(callback_id, f"Pick {', '.join(missing)} first.", alert=True)
            return
        # Claim the draft atomically: pending -> confirming. A second Confirm tap
        # (double-tap, or Telegram redelivering the callback) sees the condition
        # fail and stops here, so exactly one GitHub issue is ever created.
        try:
            drafts_table.update_item(
                Key={"draft_id": draft_id},
                UpdateExpression="SET #s = :new",
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":new": "confirming", ":pending": "pending"},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                answer_callback(callback_id, "Already filing this one.")
                return
            raise
        try:
            url = create_issue(draft)
        except Exception as e:  # noqa: BLE001 - any GitHub failure releases the claim
            print("create_issue failed:", e)
            drafts_table.update_item(
                Key={"draft_id": draft_id},
                UpdateExpression="SET #s = :s",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "pending"},
            )
            answer_callback(callback_id, "GitHub error, check logs. Tap Confirm to retry.", alert=True)
            return
        drafts_table.update_item(
            Key={"draft_id": draft_id},
            UpdateExpression="SET #s = :s, issue_url = :u",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "confirmed", ":u": url},
        )
        _telegram("editMessageText", {
            "chat_id": draft["chat_id"], "message_id": draft["message_id"],
            "text": f"\u2705 Filed: {url}",
        })
        answer_callback(callback_id)
        return

    # Re-render keyboard for any field change that didn't already return.
    _telegram("editMessageReplyMarkup", {
        "chat_id": draft["chat_id"],
        "message_id": draft["message_id"],
        "reply_markup": build_keyboard(draft),
    })


def _parse_body(event):
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    return json.loads(body)


def lambda_handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if headers.get("x-telegram-bot-api-secret-token") != WEBHOOK_SECRET:
        return {"statusCode": 401, "body": "bad secret"}

    update = _parse_body(event)
    if _already_seen(update.get("update_id")):
        return {"statusCode": 200, "body": "duplicate"}

    # Telegram re-delivers any update that doesn't get a 2xx, forever. A bug
    # in our handling of one update must not turn into an infinite retry loop,
    # so everything below is logged and acknowledged rather than raised.
    try:
        _handle_update(update)
    except Exception as e:  # noqa: BLE001 - deliberate catch-all at the boundary
        print(f"update {update.get('update_id')} failed: {type(e).__name__}: {e}")
    return {"statusCode": 200, "body": "ok"}


def _already_seen(update_id):
    """Idempotency gate. Telegram redelivers an update whenever it doesn't get a
    2xx quickly (Lambda cold start, a slow GitHub call, a transient error), and
    the redelivery carries the same update_id. Record each id with a conditional
    write in the drafts table; the second delivery fails the condition and is
    dropped, so one message creates one draft and one tap does one thing."""
    if update_id is None:
        return False
    try:
        drafts_table.put_item(
            Item={
                "draft_id": f"upd:{update_id}",
                "status": "seen",
                "ttl": int(time.time()) + 24 * 3600,
            },
            ConditionExpression="attribute_not_exists(draft_id)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            print(f"duplicate update {update_id} ignored")
            return True
        # Any other DynamoDB failure: processing once too often beats never.
        print(f"dedupe write failed for {update_id}: {e}")
    return False


def _handle_update(update):
    if "callback_query" in update:
        cq = update["callback_query"]
        if cq["from"]["id"] != ALLOWED_USER_ID:
            return
        handle_callback(cq)
        return

    message = update.get("message")
    if not message or message["from"]["id"] != ALLOWED_USER_ID:
        return

    text = message.get("text")
    if not text:
        _telegram("sendMessage", {
            "chat_id": message["chat"]["id"],
            "text": "Text only for now, please.",
        })
        return

    chat_id = message["chat"]["id"]
    if text.split(maxsplit=1)[0].split("@")[0].lower() == "/digest":
        handle_digest_command(chat_id)
        return

    pending_edit = find_awaiting_edit_draft(chat_id)
    if pending_edit:
        handle_edit_message(pending_edit, text)
    else:
        handle_new_message(chat_id, text)

