variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
}

variable "project_name" {
  type    = string
  default = "anyissues"
}

variable "github_owner" {
  description = "Your GitHub username (or org) that owns the tracked repos"
  type        = string
}

variable "github_pat" {
  description = <<-EOT
    Fine-grained PAT. Needs Issues:write, Metadata:read, Contents:read on
    every tracked repo + the braindump repo. Used ONLY to authenticate the
    `github` provider itself (topic-free repo search + label sync at plan
    time) -- it is never written into a tracked resource attribute, so it
    never lands in terraform.tfstate. The Lambdas read their own copy from
    SSM Parameter Store (see ssm.tf); re-provision that copy separately if
    you rotate this PAT.
  EOT
  type        = string
  sensitive   = true
}

variable "enable_ci_deploy" {
  description = "Create the GitHub Actions OIDC deploy role + S3 state bucket (cicd.tf). Off = deploy from your machine with local state."
  type        = bool
  default     = false
}

variable "ci_github_repo" {
  description = "Name of the repo (under github_owner) whose main-branch Actions runs may assume the deploy role. Only used when enable_ci_deploy = true."
  type        = string
  default     = "AnyIssues"
}

variable "braindump_repo" {
  description = "Repo name (no owner prefix) that eureka ideas are filed into"
  type        = string
  default     = "braindump"
}

variable "allowed_telegram_user_id" {
  description = "Your numeric Telegram user ID. Only this user can talk to the bot. Get it from @userinfobot."
  type        = number
}

variable "anthropic_model" {
  description = "Anthropic model id used for shaping tickets and writing the digest"
  type        = string
  default     = "claude-sonnet-5"
}

variable "draft_ttl_hours" {
  description = "Abandoned drafts (never confirmed/discarded) auto-expire from DynamoDB after this many hours"
  type        = number
  default     = 24
}

variable "digest_schedule" {
  description = "EventBridge Scheduler expression for the outstanding-work digest"
  type        = string
  default     = "cron(0 18 * * ? *)" # daily at 18:00
}

variable "digest_timezone" {
  description = "IANA timezone the digest schedule is evaluated in"
  type        = string
  default     = "Asia/Singapore"
}

variable "digest_enabled" {
  description = "Set false to keep the digest Lambda (for /digest) but pause the schedule"
  type        = bool
  default     = true
}

# --- Anthropic Workload Identity Federation (shape_worker + digest) ---
# None of these is a credential: holding all of them still doesn't let
# anyone mint a token without also assuming one of this deployment's IAM
# roles, which the federation rule's subject pins to. They do identify the
# deployment's Anthropic org, though, so they're set in terraform.tfvars /
# Actions secrets rather than committed as defaults. Leave the three
# required ones empty to skip WIF: the Lambdas then fall back to the
# /<project>/anthropic-api-key SSM parameter.
variable "anthropic_federation_rule_id" {
  description = "Federation rule (fdrl_...) from Claude Console -> Settings -> Workload identity. Empty = use the SSM API key instead."
  type        = string
  default     = ""
  sensitive   = true
}

variable "anthropic_organization_id" {
  description = "Anthropic organization ID the federation rule belongs to."
  type        = string
  default     = ""
  sensitive   = true
}

variable "anthropic_service_account_id" {
  description = "Service account (svac_...) the minted token acts as."
  type        = string
  default     = ""
  sensitive   = true
}

variable "anthropic_workspace_id" {
  description = "Workspace (wrkspc_...) to scope the token to. Only required if the rule spans multiple workspaces; empty for a single-workspace rule."
  type        = string
  default     = ""
  sensitive   = true
}
