# AnyIssue — Claude Code Task Brief

> **Status: historical.** This is the original brief the project was built from with Claude Code,
> kept as a record of the spec-driven workflow. All four target-state items are done. A few "locked"
> decisions were later revisited on purpose: project buttons now come from *every* non-fork,
> non-archived repo (no `anyissue-tracked` topic), secrets moved from Secrets Manager to SSM
> Parameter Store, the Anthropic call authenticates via Workload Identity Federation, and deploys
> run from GitHub Actions. See [`README.md`](README.md) and
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the current state.

## Objective

Continue building `AnyIssue`, a personal Telegram → GitHub Issues capture pipeline on AWS. A working skeleton exists; harden it, then add the outstanding-work digest. Do not redesign what is already decided.

## Context

**What this is:** I message a Telegram bot with a raw unstructured thought. An LLM shapes it into a titled ticket with acceptance criteria. I tap inline buttons to pick project / type / urgency, hit Confirm, and it becomes a GitHub issue in the relevant code repo — so coding agents can later pick it up with full repo context.

### Locked decisions — do not revisit

- Tracker: GitHub Issues only. Issues live in the actual code repos, not a central tracker repo.
- `braindump` repo is the catch-all for "eureka" ideas (new app concepts with no repo yet). Single shared repo — NOT one new repo per eureka.
- Text input only. Voice notes and image/screenshot attachments were explicitly dropped for v1.
- Confirm step is required before any issue is created — preview message with buttons, never fire-and-forget.
- Project buttons are fetched live from GitHub at shape time via topic search (`user:<owner> topic:anyissue-tracked`). No project registry table. Adding a repo = adding the topic in GitHub settings, zero redeploy.
- IaC: Terraform. LLM: Anthropic API direct (not Bedrock). GitHub auth: fine-grained PAT.
- Serverless and event-driven, not a long-running container.

### Current stack (built, compiles, NOT yet run against live APIs)

    Telegram → API Gateway (HTTP API, POST /webhook)
      → webhook_handler Lambda   (verify secret token, gate on ALLOWED_USER_ID,
                                   send "thinking..." placeholder, write draft, enqueue)
      → SQS (+ DLQ, maxReceiveCount 3)
      → shape_worker Lambda      (Anthropic call → {title, body}; GitHub topic search
                                   → repo list; edit placeholder into preview + keyboard)
      → [button taps] → webhook_handler (synchronous: mutate draft, re-render keyboard)
      → Confirm → GitHub issue created → message edited with issue URL

Two Lambdas, not three — button taps arrive on the same API Gateway route and are fast enough not to need queueing. Python 3.12, **stdlib only** (`urllib`, `json`, `boto3`) so `terraform apply` needs no pip/build step.

### State

One DynamoDB table `drafts`, hash key `draft_id` (8-char hex), PAY_PER_REQUEST, TTL attribute `ttl`.

Fields: `chat_id`, `message_id` (the bot's own preview message, edited in place), `status` (processing | pending | awaiting_edit | confirmed | discarded), `raw_text`, `llm_title`, `llm_body`, `repos_snapshot` (list), `project`, `type`, `urgency`, `created_at`, `ttl`.

### callback_data scheme

Telegram caps this at 64 bytes — keep it terse.

    d:<draft_id>:p:<index>    project (index into repos_snapshot + ["eureka"])
    d:<draft_id>:t:<bug|feature>
    d:<draft_id>:u:<asap|normal|good-to-have>
    d:<draft_id>:c            confirm
    d:<draft_id>:e            edit
    d:<draft_id>:x            discard

Selecting `eureka` as project auto-sets `type=eureka` and hides the type row. Confirm returns an `answerCallbackQuery` alert listing missing fields rather than proceeding if project/urgency (and type, when not eureka) aren't all set. Edit sets `status=awaiting_edit`; the next plain-text message is treated as a correction to that same draft and re-enqueued.

### Labels

Managed declaratively in Terraform via `github_issue_label` across all tracked repos + braindump: `bug`, `feature`, `eureka`, `urgency:asap`, `urgency:normal`, `urgency:good-to-have`.

### Repo layout

    terraform/   versions.tf variables.tf secrets.tf dynamodb.tf sqs.tf
                 iam.tf lambda.tf apigateway.tf github_labels.tf outputs.tf
                 terraform.tfvars.example
    lambdas/webhook_handler/handler.py
    lambdas/shape_worker/handler.py
    README.md

## Target State

Work these in order. Stop after each numbered item and report before continuing.

1. **Verify the skeleton actually runs.** It has never been executed against live Telegram / GitHub / Anthropic APIs. Find and fix real breakage.
2. **Fix the known weak points** (listed under Constraints below).
3. **Add tests.** There are currently none.
4. **Build the digest feature** — a third Lambda on an EventBridge Scheduler cron that reads open issues across all tracked repos, summarizes outstanding work with the Anthropic API, and messages me on Telegram asking which to start first. Support both scheduled push and an on-demand `/digest` command.

## Scope

- Work only in: `terraform/`, `lambdas/`, `README.md`
- Do NOT touch: `terraform.tfvars` (real secrets), `.terraform/`, `terraform.tfstate*`, `build/`
- Do NOT run `terraform apply` or any command that creates AWS resources or calls the GitHub/Telegram APIs with real credentials. `terraform validate` and `terraform plan` are fine if credentials are absent; `terraform fmt` is fine.

## Constraints

- Python 3.12, **stdlib + boto3 only**. No `requests`, no `python-telegram-bot`, no Anthropic SDK. If you believe a dependency is genuinely necessary, stop and ask first.
- Terraform ≥1.5, AWS provider ~5.0, GitHub provider ~6.0.
- Keep the two-Lambda shape. Do not split handlers or introduce Step Functions, VPCs, API Gateway authorizers, or a web dashboard.
- Preserve the 64-byte `callback_data` budget in any new buttons.
- Only make changes directly requested. Do not add features, abstractions, or files beyond what is asked.

### Known weak points to address in item 2

- `find_awaiting_edit_draft` uses a DynamoDB `scan`. Acceptable at single-user volume — either justify keeping it in a code comment or replace with a GSI on `(chat_id, status)`. Pick one and say why.
- Telegram `parse_mode: Markdown` (legacy) will fail on unescaped `_ * [ ]` in LLM-generated titles/bodies. Fix properly — either `MarkdownV2` with full escaping, or plain text.
- `shape_with_llm` assumes the model returns clean JSON. Strengthen parsing and the failure path.
- No idempotency on Telegram redelivery. Telegram retries webhooks; a duplicate `update_id` should not create a second draft or a second issue. Double-tapping Confirm must not file twice.
- Secrets are cached in a module-level dict for the Lambda container's lifetime — confirm this is safe for rotation or note the tradeoff.

## Acceptance Criteria

- [ ] `terraform validate` passes and `terraform fmt -check` is clean
- [ ] Both existing handlers compile and pass their new unit tests
- [ ] Telegram/GitHub/Anthropic HTTP calls are mocked in tests — no live network calls in the suite
- [ ] A duplicate Telegram `update_id` provably creates exactly one draft
- [ ] Two rapid Confirm taps on one draft provably create exactly one GitHub issue
- [ ] LLM output containing `_`, `*`, `[`, `]` renders without a Telegram API error
- [ ] Digest Lambda exists with Terraform wiring, IAM, schedule, and tests
- [ ] `/digest` command returns the same summary on demand
- [ ] README updated to match the final state, including digest setup

## Action Boundaries

- Proceed with reversible, in-scope edits, reads, `terraform validate`/`fmt`/`plan`, and running tests.
- Stop and ask before: adding any dependency, changing the locked decisions above, modifying IAM to widen permissions, any command that provisions AWS resources or writes to GitHub/Telegram.
- Ask me for anything only I can supply (real repo names, my Telegram user ID, tokens). Never invent placeholder credentials that look real.

## Progress Evidence

Report at each of the four numbered checkpoints, not continuously. Ground every completion claim in an actual tool result — test output, `terraform validate` output, or the diff. If something is untestable without live credentials, say so explicitly rather than asserting it works.

## Session Strategy

Continue — prior context still needed.
