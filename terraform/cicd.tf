# OPTIONAL -- only created when var.enable_ci_deploy = true. Leave it off
# for a plain `terraform apply` from your laptop with local state.
#
# When on: GitHub Actions deploys this project on every push to main (see
# .github/workflows/deploy.yml). Auth is OIDC: GitHub mints a short-lived
# token, exchanged here for temporary credentials scoped to exactly this
# repo's main branch. No static AWS keys anywhere, same reasoning as the
# Anthropic WIF setup in the Lambda handlers. Also creates the S3 bucket the
# workflow keeps Terraform state in (see backend.tf.example).

# An IAM OIDC provider is one-per-URL-per-AWS-account -- this account
# already has one for GitHub Actions (another project's Terraform manages
# it). Reference it rather than trying to own/recreate it here. If your
# account has none yet, create it once -- see README "Continuous deployment".
data "aws_iam_openid_connect_provider" "github_actions" {
  count = var.enable_ci_deploy ? 1 : 0
  url   = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "github_actions_assume" {
  count = var.enable_ci_deploy ? 1 : 0
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github_actions[0].arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Only workflow runs triggered by a push to main can assume this role --
    # not pull requests (which may run untrusted, forked code). StringLike
    # (not StringEquals) because this account has GitHub's immutable-ID
    # subject format on: the real claim is
    # "repo:<owner>@<owner_id>/<repo>@<repo_id>:ref:refs/heads/main", not
    # the plain "repo:<owner>/<repo>:ref:..." the docs lead with.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_owner}@*/${var.ci_github_repo}@*:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_actions_deploy" {
  count              = var.enable_ci_deploy ? 1 : 0
  name               = "${var.project_name}-github-actions-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume[0].json
}

locals {
  aws_account_id = data.aws_caller_identity.current.account_id
  project_arn_prefix = {
    lambda    = "arn:aws:lambda:${var.aws_region}:${local.aws_account_id}:function:${var.project_name}-"
    iam_role  = "arn:aws:iam::${local.aws_account_id}:role/${var.project_name}-"
    logs      = "arn:aws:logs:${var.aws_region}:${local.aws_account_id}:log-group:/aws/lambda/${var.project_name}-"
    sqs       = "arn:aws:sqs:${var.aws_region}:${local.aws_account_id}:${var.project_name}-"
    scheduler = "arn:aws:scheduler:${var.aws_region}:${local.aws_account_id}:schedule/default/${var.project_name}-"
  }
}

# Scoped to exactly the resource types and name prefixes this config
# manages -- not AdministratorAccess. Update alongside iam.tf/lambda.tf/etc
# when this Terraform starts managing a new AWS resource type.
resource "aws_iam_role_policy" "github_actions_deploy" {
  count = var.enable_ci_deploy ? 1 : 0
  name  = "${var.project_name}-github-actions-deploy"
  role  = aws_iam_role.github_actions_deploy[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Dedicated single-purpose bucket for this project's state -- s3:*
        # rather than chasing every read action the aws_s3_bucket resource's
        # refresh touches (ACL, policy, versioning, tagging, ...).
        Sid      = "TerraformStateBucket"
        Effect   = "Allow"
        Action   = ["s3:*"]
        Resource = ["${aws_s3_bucket.terraform_state[0].arn}/*", aws_s3_bucket.terraform_state[0].arn]
      },
      {
        Sid      = "DynamoDbDraftsTable"
        Effect   = "Allow"
        Action   = ["dynamodb:*"]
        Resource = aws_dynamodb_table.drafts.arn
      },
      {
        Sid    = "LambdaFunctions"
        Effect = "Allow"
        Action = ["lambda:*"]
        # Event source mapping ARNs are opaque UUIDs, not tied to the
        # function name, so they can't share the project's name-prefix scope.
        Resource = [
          "${local.project_arn_prefix.lambda}*",
          "arn:aws:lambda:${var.aws_region}:${local.aws_account_id}:event-source-mapping:*",
        ]
      },
      {
        Sid      = "IamRolesAndPolicies"
        Effect   = "Allow"
        Action   = ["iam:*"]
        Resource = "${local.project_arn_prefix.iam_role}*"
      },
      {
        # The data source that looks up the account's existing GitHub OIDC
        # provider by URL: List has no resource-level permissions support,
        # Get is scoped to that one provider once List has found its ARN.
        Sid      = "ListOidcProviders"
        Effect   = "Allow"
        Action   = ["iam:ListOpenIDConnectProviders"]
        Resource = "*"
      },
      {
        Sid      = "GetOidcProvider"
        Effect   = "Allow"
        Action   = ["iam:GetOpenIDConnectProvider"]
        Resource = data.aws_iam_openid_connect_provider.github_actions[0].arn
      },
      {
        Sid      = "PassProjectRolesToLambdaAndScheduler"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = "${local.project_arn_prefix.iam_role}*"
        Condition = {
          StringEquals = {
            "iam:PassedToService" = ["lambda.amazonaws.com", "scheduler.amazonaws.com"]
          }
        }
      },
      {
        Sid      = "ApiGatewayHttpApi"
        Effect   = "Allow"
        Action   = ["apigateway:*"]
        Resource = ["arn:aws:apigateway:${var.aws_region}::/apis", "arn:aws:apigateway:${var.aws_region}::/apis/*"]
      },
      {
        Sid      = "SqsQueues"
        Effect   = "Allow"
        Action   = ["sqs:*"]
        Resource = "${local.project_arn_prefix.sqs}*"
      },
      {
        Sid      = "EventBridgeScheduler"
        Effect   = "Allow"
        Action   = ["scheduler:*"]
        Resource = "${local.project_arn_prefix.scheduler}*"
      },
      {
        # The schedule resource above lives in the (default, implicit)
        # "default" schedule group; the provider reads the group itself too.
        Sid      = "EventBridgeSchedulerDefaultGroup"
        Effect   = "Allow"
        Action   = ["scheduler:GetScheduleGroup"]
        Resource = "arn:aws:scheduler:${var.aws_region}:${local.aws_account_id}:schedule-group/default"
      },
      {
        Sid      = "CloudWatchLogGroups"
        Effect   = "Allow"
        Action   = ["logs:*"]
        Resource = "${local.project_arn_prefix.logs}*"
      },
      {
        Sid      = "CloudWatchLogGroupsDescribe"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "*"
      },
      {
        Sid      = "CallerIdentity"
        Effect   = "Allow"
        Action   = ["sts:GetCallerIdentity"]
        Resource = "*"
      }
    ]
  })
}

output "github_actions_role_arn" {
  description = "Set as AWS_GITHUB_ACTIONS_ROLE_ARN in the repo's Actions secrets/vars"
  value       = one(aws_iam_role.github_actions_deploy[*].arn)
}

# ---------------- Terraform state bucket ----------------
# Created while state is still local; `terraform init -migrate-state` then
# moves state in (README "Continuous deployment"). prevent_destroy also
# means a plan that would drop it -- e.g. enable_ci_deploy accidentally left
# false on a checkout that uses it -- errors out instead.

resource "aws_s3_bucket" "terraform_state" {
  count  = var.enable_ci_deploy ? 1 : 0
  bucket = "${var.project_name}-tfstate-${local.aws_account_id}"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  count  = var.enable_ci_deploy ? 1 : 0
  bucket = aws_s3_bucket.terraform_state[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  count  = var.enable_ci_deploy ? 1 : 0
  bucket = aws_s3_bucket.terraform_state[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  count                   = var.enable_ci_deploy ? 1 : 0
  bucket                  = aws_s3_bucket.terraform_state[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

output "terraform_state_bucket" {
  value = one(aws_s3_bucket.terraform_state[*].bucket)
}

# These resources predate enable_ci_deploy; keep existing deployments'
# state addresses stable instead of destroying and recreating them.

moved {
  from = aws_iam_role.github_actions_deploy
  to   = aws_iam_role.github_actions_deploy[0]
}

moved {
  from = aws_iam_role_policy.github_actions_deploy
  to   = aws_iam_role_policy.github_actions_deploy[0]
}

moved {
  from = aws_s3_bucket.terraform_state
  to   = aws_s3_bucket.terraform_state[0]
}

moved {
  from = aws_s3_bucket_versioning.terraform_state
  to   = aws_s3_bucket_versioning.terraform_state[0]
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.terraform_state
  to   = aws_s3_bucket_server_side_encryption_configuration.terraform_state[0]
}

moved {
  from = aws_s3_bucket_public_access_block.terraform_state
  to   = aws_s3_bucket_public_access_block.terraform_state[0]
}
