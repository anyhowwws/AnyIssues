<p align="center"><img src="assets/logo.png" alt="AnyIssues logo" width="160"></p>

# AnyIssues: architecture and design notes

This covers how AnyIssues is built, why it's built that way, and what broke
along the way. For setup, see the [README](../README.md).

## At a glance

| | |
|---|---|
| **What** | A Telegram bot that turns a raw thought into a structured, labelled GitHub issue in the right repo, after a button-driven confirm. It also sends a daily Claude-written digest of everything still open. |
| **Why** | Ideas and bugs occur away from the keyboard. Capturing them should take seconds, and the resulting issues should be good enough for a coding agent to pick up with full repo context. |
| **Who** | Single user, self-hosted. The repo is MIT-licensed and designed to be forked and deployed to your own AWS account. |
| **Stack** | Python 3.12 on AWS Lambda (standard library + boto3 only), API Gateway HTTP API, SQS + DLQ, DynamoDB, EventBridge Scheduler, SSM Parameter Store, Terraform (AWS + GitHub providers), GitHub Actions with AWS OIDC, and the Claude API (structured outputs, Workload Identity Federation). |
| **Size** | 3 Lambdas (~1,300 lines total) and 73 unit tests, all mocked with no network access. About a dozen Terraform files. |
| **Status** | Live and in daily personal use since September 2026. |
| **Built with** | Claude Code, working from a written brief ([`anyissue-claude-code-prompt.md`](../anyissue-claude-code-prompt.md)) with numbered checkpoints. Each checkpoint was reported against test and `terraform validate` output. |

## The flow

```
            ┌──────────────────── capture (async) ─────────────────────┐
Telegram ──▶ API Gateway ──▶ webhook_handler ──▶ SQS ──▶ shape_worker ──▶ Claude
   ▲              (HTTP API)     │  ▲    │       (+DLQ)      │            GitHub search
   │                             │  │    ▼                   ▼
   │                             │  │  DynamoDB `drafts` ◀───┘
   │                             │  └── button taps (sync): mutate draft, re-render
   │                             └──── Confirm ──▶ GitHub: create issue
   │
   └──── digest ◀── EventBridge Scheduler (daily) / `/digest` (async invoke)
                     reads open issues across repos ──▶ Claude summary
```

1. **Message in.** `webhook_handler` checks Telegram's secret-token header,
   drops anyone who isn't the configured user, records the `update_id` for
   dedupe, posts a "thinking..." placeholder, writes a `processing` draft and
   enqueues it. This path takes milliseconds, well inside Telegram's webhook
   timeout.
2. **Shape.** `shape_worker` (triggered by SQS, batch size 1) asks Claude for
   `{title, body}` under a JSON schema. It fetches the owner's repos live from
   GitHub search and edits the placeholder into a preview with an inline
   keyboard: project, type (bug / feature), urgency (asap / normal /
   good-to-have), and Confirm / Edit / Discard.
3. **Tap.** Button taps return to `webhook_handler` and are handled
   synchronously. The handler updates the draft and re-renders the keyboard in
   place. Picking **eureka** as the project routes to the braindump repo and
   hides the type row.
4. **Confirm.** The handler atomically claims the draft, creates the issue with
   labels, and edits the message into a link. **Edit** instead parks the draft
   as `awaiting_edit`, and the next message re-shapes it.
5. **Digest.** A third Lambda lists open issues (PRs excluded) across every
   repo. It orders them by repo, with the most `urgency:asap` issues first and
   braindump last, and by urgency within each repo. Claude summarises the list,
   `repo#123` tokens become links, and the result ends by asking which one to
   start first.

## Key decisions

| Decision | Why | Trade-off accepted |
|---|---|---|
| **Two Lambdas for capture, split at an SQS queue** | An LLM call plus a GitHub search can exceed Telegram's patience, and Telegram retries slow webhooks. Queueing gives a fast acknowledgement, automatic retries and a DLQ. Button taps are fast, so they skip the queue. | One more hop, and the placeholder message is edited rather than sent fresh. |
| **Standard library + boto3 only** | No packaging step, no Lambda layers, no dependency drift. `terraform apply` zips the source directories as they are. | Hand-rolled `urllib` clients for Telegram, GitHub and Claude (about 20 lines each). |
| **Every non-fork, non-archived repo is a project** | The first version used an opt-in GitHub topic. In practice, forgetting to tag a repo meant the idea had nowhere to go. A live search means zero registration. | Labels still need a `terraform apply` for brand-new repos. |
| **Confirm step is mandatory** | An LLM shapes the ticket, but a human chooses where it goes. Nothing is filed fire-and-forget. | One extra tap. |
| **Structured outputs + defensive parsing** | The API enforces the JSON schema, and the parser still tolerates fenced or wrapped JSON. Refusals and truncation are *permanent* errors (the draft is marked `failed` and the user is told); 429/5xx are *transient* (SQS retries, then the DLQ). | None worth noting. |
| **Telegram `parse_mode=HTML` + `html.escape`** | Legacy Markdown mode fails on the `_ * [ ]` that LLM output is full of. HTML escaping is complete and simple. | None. |
| **DynamoDB Scan for the pending-edit lookup** | The table only holds one user's live drafts, because TTL evicts the rest. A GSI on `(chat_id, status)` would add write amplification and eventual-consistency edge cases for no gain. | Revisit if this ever becomes multi-user. |
| **Secrets in SSM, never in Terraform state** | This replaced Secrets Manager, whose `secret_version` resource puts the plaintext credential in `terraform.tfstate`. SSM parameters are created out-of-band, and Terraform only interpolates their *paths*. Deliberately, there is no `data "aws_ssm_parameter"` either, because that also writes the decrypted value into state. | Terraform no longer checks that the parameters exist. Also removes Secrets Manager's per-secret monthly fee. |
| **API key by default, Workload Identity Federation optional** | A new user gets running with one SSM-stored API key. WIF is an opt-in upgrade: each Lambda gets an AWS-signed JWT for its own IAM role from `sts:GetWebIdentityToken` and exchanges it for a short-lived Anthropic token. Terraform outputs the exact CEL match expression for the federation rule, so nobody hand-builds role ARNs. | WIF failures fall back to the key silently, so the README tells users to check the logs once. |
| **GitHub Actions → AWS via OIDC** | No stored AWS keys. The deploy role trusts only `main` of this repo, never PRs from forks, and its policy is scoped to this project's resource-name prefixes rather than admin. | Opt-in (`enable_ci_deploy`), so forks can deploy from a laptop with no bootstrap. |

## Reliability and correctness

- **Exactly-once capture.** Telegram redelivers any update not acknowledged
  quickly. A conditional put on `upd:<update_id>` drops the duplicates.
- **Exactly-once filing.** Confirm claims the draft `pending → confirming`
  with a conditional update, so a double tap files one issue. If the GitHub
  call fails, the claim is released so you can retry.
- **No retry storms.** The webhook always returns 200 after logging a handler
  error, so one bad update can't loop forever.
- **The digest always arrives.** If the Claude call fails, it falls back to a
  plain grouped list. It's also split to fit Telegram's 4,096-character
  limit.
- **Credential rotation without a deploy.** Secrets are cached per container,
  and any 401/403 evicts the cached value, so a rotated secret is picked up on
  the next call.

## Security model

- **Webhook:** Telegram's secret-token header, generated by Terraform. There
  is also a hard gate on one numeric Telegram user ID.
- **IAM:** each Lambda has its own role, scoped to the exact table, queue,
  parameter ARNs and function it uses.
- **State:** Terraform state never holds the bot token, PAT or Claude key (see
  the SSM decision above). The only secret in it is the webhook secret
  Terraform generates. Remote state lives in a versioned, encrypted S3 bucket with public
  access blocked and `prevent_destroy` set.
- **Public repo hygiene.** Nothing that identifies the deployment is
  committed: account ID, state bucket, role ARNs, webhook URL, Anthropic
  org/service-account IDs, Telegram user ID. CI gets these as masked secrets.
  The deploy job prints only plan/apply summary lines, because full Terraform
  output lists a label resource for every repo the owner has, private ones
  included.

## Lessons learned

Most of these were caught only in production, because unit tests mock the
cloud:

- **The tests passed, but IAM was missing `dynamodb:Scan`.** The pending-edit
  lookup had used a Scan since day one, and every test mocked the table. The
  first real message hit `AccessDenied`, which the catch-all logged and
  acknowledged, so nothing visibly happened. *Lesson:* mocks prove the logic,
  not the permissions. The first live smoke test must exercise every AWS call.
- **DynamoDB reserved words go beyond the obvious ones.** `ttl`, `type` and
  `status` were aliased, but `project` was not, and the `REMOVE` clause failed
  silently. The test harness now validates expressions against DynamoDB's
  *full* reserved-word list.
- **Non-deterministic zips caused perpetual drift.** `archive_file` embeds
  file modification times, so a CI checkout always differed from a local one
  and every plan wanted to redeploy all three functions. A small zip builder
  with fixed timestamps and permissions (and no `__pycache__`) fixed it.
- **GitHub's OIDC subject claim isn't always what the documentation shows.**
  This account uses GitHub's immutable-ID format
  (`repo:owner@<id>/repo@<id>:ref:...`), so the trust policy's `StringEquals`
  on `repo:owner/repo:...` never matched. The fix was `StringLike` with
  wildcards on the IDs.
- **`terraform plan` needs more read permissions than apply.** Refreshing
  state touches bucket ACLs, the schedule group and the OIDC provider. The
  CI role was widened read by read, still scoped to project prefixes.
- **A silent fallback hid a broken WIF setup for weeks.** Every token
  exchange was refused, and every call quietly used the SSM key, so nothing
  looked wrong. The Console's authentication events gave two reasons. The
  rule's subject pattern named only the `shape-worker` role, so `digest`
  failed `match_subject_prefix`. The rule was also enabled in two
  workspaces, so `shape-worker` failed `workspace_id_required`. *Lesson:* a
  fallback needs a visible signal. Terraform now outputs a CEL expression
  covering both roles, and the README has users confirm the log line
  `anthropic auth: workload identity federation` once.
- **Public CI logs are part of the attack surface.** Before this repo went
  public, the deploy logs contained the account ID, the live webhook URL and
  the names of private repos, via the label resources. Log hygiene is now part
  of the design.

## Testing

73 `unittest` tests (standard library only: no pytest, no moto). The harness
fakes the DynamoDB table, SQS, SSM, STS and Lambda clients, and routes every
`urllib` call to Telegram, GitHub or Anthropic to a scripted responder. The
responder records every request and can simulate an HTTP failure per host. Covered: duplicate `update_id` produces
one draft; double Confirm produces one issue; Markdown-hostile LLM output
renders; permanent versus transient LLM failures; WIF token exchange and
fallback; digest ordering, linkifying and chunking; reserved-word aliasing.
CI runs the suite plus `terraform fmt`/`validate` on every PR.

## Roadmap

- Auto-trigger a coding agent when a bug is filed, with a running environment
  to reproduce it in ([#2](https://github.com/anyhowwws/AnyIssues/issues/2)).
- Surface WIF fallback more loudly (for example a CloudWatch metric or a
  one-time Telegram notice) rather than only a log line.
- Voice and image capture were cut from v1 on purpose, and could come back.
