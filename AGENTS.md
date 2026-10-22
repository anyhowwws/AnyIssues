# AnyIssues

Personal Telegram → GitHub Issues capture bot. Raw thought in, LLM-shaped ticket out,
button-driven confirm, issue filed in the relevant code repo. Single user. The full task
brief and locked design decisions live in `anyissue-claude-code-prompt.md` — read it before
changing behaviour. Where the brief and the code disagree (it predates the switch from topic
opt-in to "every repo is a project", SSM, and WIF), the code and `docs/ARCHITECTURE.md` win.

## Stack (locked — see the brief)

- Python 3.12 Lambdas, **stdlib + boto3 only**. No `requests`, no SDKs. Ask before adding any dependency.
- Terraform ≥1.5 (AWS ~5.0, GitHub ~6.0), region `ap-southeast-1`. Two Lambdas for capture
  (`webhook_handler`, `shape_worker`) + `digest` on EventBridge Scheduler (also async-invoked for `/digest`). No Step Functions,
  VPCs, authorizers, dashboards.
- Anthropic API called directly over `urllib`. Default auth is an SSM-stored API key; Workload Identity
  Federation (AWS STS JWT -> short-lived Anthropic token) is opt-in and falls back to that key. The rule
  must accept both Lambda roles: `terraform output anthropic_wif_cel_expression`. GitHub via fine-grained
  PAT. Secrets in SSM Parameter Store (SecureString, provisioned out-of-band -- see terraform/ssm.tf),
  never in terraform.tfstate.
- Deploys: GitHub Actions on push to main, AWS via OIDC (terraform/cicd.tf), state in S3.
- State: one DynamoDB table `drafts` (hash key `draft_id`, TTL attr `ttl`).

## Layout

    lambdas/<function>/handler.py   one self-contained handler per Lambda; each dir is zipped as-is
    lambdas/tests/                  unittest suite (kept outside the zipped dirs)
    terraform/                      all infra; terraform.tfvars + backend.tf are local-only (gitignored)
    docs/ARCHITECTURE.md            design decisions, trade-offs, lessons learned

## Commands

    .venv/bin/python -m unittest discover -s lambdas/tests -v   # tests; .venv = python3.12 + boto3 only
    .venv/bin/python -m py_compile lambdas/*/handler.py
    cd terraform && terraform init -backend=false && terraform validate && terraform fmt -check
    cd terraform && terraform init -backend-config="bucket=<state bucket>"   # real backend (needs backend.tf), for plan/apply

## Rules a fresh session gets wrong

- Project buttons = every non-fork, non-archived repo the owner has (live GitHub search, `sort=updated`),
  minus braindump. There is no topic/registry; don't reintroduce one.

- `python3` on this Mac is 3.9 (anaconda). Use `.venv/bin/python` (3.12 + boto3); create it with `/opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install boto3`.
- Tests use stdlib `unittest` + `unittest.mock` only — no pytest, no moto. All Telegram/GitHub/
  Anthropic HTTP is mocked; never hit live APIs.
- **This repo is public, and so are its Actions logs.** Never commit (or print in CI) anything that
  identifies the deployment: AWS account ID, state bucket name, role ARNs, webhook URL, Anthropic
  org / service-account / federation-rule IDs, Telegram user ID, private repo names. These live in
  `terraform.tfvars`, `-backend-config`, and Actions *secrets* (not variables). The deploy job prints only
  plan/apply summary lines for this reason -- keep it that way.
- The repo is meant to be forked and self-hosted: keep anything owner-specific in variables, not
  hardcoded. CI/CD + remote state (`cicd.tf`) is opt-in via `enable_ci_deploy`; this deployment
  has it on (tfvars + workflow), and the state bucket has `prevent_destroy`.
- This project is deployed live (ap-southeast-1, `AWS_PROFILE=terraform`; account ID in your AWS profile).
  `terraform apply`/`plan` with real creds is now in scope -- but never touch `terraform.tfvars`,
  `.terraform/`, `*.tfstate*`, `build/` directly, and always show the plan before applying.
- Secrets (bot token, GitHub PAT copy, Anthropic key) live in **SSM Parameter Store**
  (`SecureString`, paths in `terraform/ssm.tf`), provisioned out-of-band via `aws ssm put-parameter`
  -- never as a Terraform resource, never via `data "aws_ssm_parameter"` (that pulls the decrypted
  value into `terraform.tfstate`, defeating the point). Don't reintroduce Secrets Manager for these.
  `github_pat` is still a Terraform variable, but only to authenticate the `github` provider itself.
- DynamoDB: `ttl`, `type`, `status` are reserved words — always alias via `ExpressionAttributeNames`.
  Items read via the boto3 resource come back as `Decimal`; never `json.dumps` them raw.
- Telegram messages use `parse_mode: "HTML"` with `html.escape`. `callback_data` ≤ 64 bytes.
- Don't add helpers shared across Lambda dirs — each `handler.py` is deliberately self-contained
  because `terraform/scripts/build_lambda_zip.py` zips one directory per function.
- Work only in `terraform/`, `lambdas/`, `.github/`, `README.md`, `docs/`. Report at the brief's numbered checkpoints,
  grounded in tool output; say explicitly what is untestable without live credentials.
