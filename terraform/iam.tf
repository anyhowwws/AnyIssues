data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# ---------------- webhook_handler ----------------
# Handles both new text messages (fast ack -> SQS) and button taps
# (fast, synchronous DynamoDB update + Telegram edit).
resource "aws_iam_role" "webhook_handler" {
  name               = "${var.project_name}-webhook-handler"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "webhook_basic_logs" {
  role       = aws_iam_role.webhook_handler.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "webhook_handler" {
  name = "${var.project_name}-webhook-handler"
  role = aws_iam_role.webhook_handler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        # Scan backs find_awaiting_edit_draft. Missing since the original
        # deploy -- unnoticed until the first live message actually hit an
        # awaiting_edit lookup, because every prior check (unit tests, the
        # webhook-gate curl probe) mocks or short-circuits before reaching it.
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Scan"]
        Resource = aws_dynamodb_table.drafts.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.shape_queue.arn
      },
      {
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = [
          local.ssm_arn.telegram_bot_token,
          local.ssm_arn.github_pat,
        ]
      },
      {
        # /digest: fire-and-forget the digest Lambda. Scoped to that one function.
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.digest.arn
      }
    ]
  })
}

# ---------------- shape_worker ----------------
# SQS-triggered. Calls Anthropic + GitHub search, writes the draft, edits
# the Telegram placeholder message into the real preview with buttons.
resource "aws_iam_role" "shape_worker" {
  name               = "${var.project_name}-shape-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "shape_basic_logs" {
  role       = aws_iam_role.shape_worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "shape_sqs" {
  role       = aws_iam_role.shape_worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaSQSQueueExecutionRole"
}

resource "aws_iam_role_policy" "shape_worker" {
  name = "${var.project_name}-shape-worker"
  role = aws_iam_role.shape_worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.drafts.arn
      },
      {
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = [
          local.ssm_arn.telegram_bot_token,
          local.ssm_arn.anthropic_api_key,
          local.ssm_arn.github_pat,
        ]
      },
      {
        # Mints the AWS-signed JWT exchanged for an Anthropic access token
        # (see lambdas/shape_worker/handler.py). GetWebIdentityToken has no
        # resource-level permissions -- the federation rule itself is what
        # scopes this to exactly this role's ARN, nothing broader.
        Effect   = "Allow"
        Action   = ["sts:GetWebIdentityToken"]
        Resource = "*"
      }
    ]
  })
}

# ---------------- digest ----------------
# Reads open issues from GitHub, summarises with Anthropic, messages Telegram.
# No DynamoDB or SQS access: it is stateless.
resource "aws_iam_role" "digest" {
  name               = "${var.project_name}-digest"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "digest_basic_logs" {
  role       = aws_iam_role.digest.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "digest" {
  name = "${var.project_name}-digest"
  role = aws_iam_role.digest.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = [
          local.ssm_arn.telegram_bot_token,
          local.ssm_arn.anthropic_api_key,
          local.ssm_arn.github_pat,
        ]
      },
      {
        # See the matching comment on shape_worker's policy above.
        Effect   = "Allow"
        Action   = ["sts:GetWebIdentityToken"]
        Resource = "*"
      }
    ]
  })
}

# ---------------- EventBridge Scheduler ----------------
# The schedule assumes this role to invoke the digest Lambda.
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project_name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${var.project_name}-scheduler"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.digest.arn
      }
    ]
  })
}
