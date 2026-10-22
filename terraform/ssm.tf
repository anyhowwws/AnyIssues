# The bot token, GitHub PAT, and Anthropic key are NOT Terraform resources.
# They are provisioned out-of-band as SSM SecureString parameters:
#
#   aws ssm put-parameter --name /anyissues/telegram-bot-token \
#     --type SecureString --overwrite --value "<token>"
#
# (same for /anyissues/github-pat and /anyissues/anthropic-api-key)
#
# Terraform is given only the *paths* below, never the values, and never a
# `data "aws_ssm_parameter"` lookup -- that data source resolves and writes
# the decrypted value into terraform.tfstate even when only the name is
# referenced, which defeats the point. ARNs are built by string
# interpolation instead: paths are not secret (they're right here in this
# repo), so this loses nothing except Terraform's own existence check on the
# parameter, in exchange for terraform.tfstate never holding a credential.
#
# Rotating a secret is now: re-run the put-parameter command above with the
# new value. No terraform apply needed; Lambda containers pick it up on
# their next 401/403-triggered cache eviction or next cold start.
data "aws_caller_identity" "current" {}

locals {
  ssm = {
    telegram_bot_token = "/${var.project_name}/telegram-bot-token"
    github_pat         = "/${var.project_name}/github-pat"
    anthropic_api_key  = "/${var.project_name}/anthropic-api-key"
  }

  ssm_arn = {
    for key, path in local.ssm :
    key => "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${path}"
  }
}
