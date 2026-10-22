# Zips each lambdas/<function> dir with a deterministic zip builder instead
# of `archive_file` -- see scripts/build_lambda_zip.py for why: archive_file
# embeds source file mtimes in the zip, so source_code_hash never stabilised
# across a local checkout vs. a CI checkout of the same commit.
data "external" "webhook_handler_zip" {
  program = ["python3", "${path.module}/scripts/build_lambda_zip.py"]
  query = {
    source_dir  = "${path.module}/../lambdas/webhook_handler"
    output_path = "${path.module}/build/webhook_handler.zip"
  }
}

data "external" "shape_worker_zip" {
  program = ["python3", "${path.module}/scripts/build_lambda_zip.py"]
  query = {
    source_dir  = "${path.module}/../lambdas/shape_worker"
    output_path = "${path.module}/build/shape_worker.zip"
  }
}

data "external" "digest_zip" {
  program = ["python3", "${path.module}/scripts/build_lambda_zip.py"]
  query = {
    source_dir  = "${path.module}/../lambdas/digest"
    output_path = "${path.module}/build/digest.zip"
  }
}

resource "aws_cloudwatch_log_group" "webhook_handler" {
  name              = "/aws/lambda/${var.project_name}-webhook-handler"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "shape_worker" {
  name              = "/aws/lambda/${var.project_name}-shape-worker"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "digest" {
  name              = "/aws/lambda/${var.project_name}-digest"
  retention_in_days = 14
}

resource "aws_lambda_function" "webhook_handler" {
  function_name    = "${var.project_name}-webhook-handler"
  role             = aws_iam_role.webhook_handler.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 10
  filename         = data.external.webhook_handler_zip.result.output_path
  source_code_hash = data.external.webhook_handler_zip.result.sha256_base64
  depends_on       = [aws_cloudwatch_log_group.webhook_handler]

  environment {
    variables = {
      DRAFTS_TABLE             = aws_dynamodb_table.drafts.name
      SHAPE_QUEUE_URL          = aws_sqs_queue.shape_queue.url
      WEBHOOK_SECRET           = random_password.webhook_secret.result
      ALLOWED_USER_ID          = tostring(var.allowed_telegram_user_id)
      DRAFT_TTL_HOURS          = tostring(var.draft_ttl_hours)
      BRAINDUMP_REPO           = var.braindump_repo
      GITHUB_OWNER             = var.github_owner
      TELEGRAM_BOT_TOKEN_PARAM = local.ssm.telegram_bot_token
      GITHUB_PAT_PARAM         = local.ssm.github_pat
      DIGEST_FUNCTION_NAME     = aws_lambda_function.digest.function_name
    }
  }
}

resource "aws_lambda_function" "shape_worker" {
  function_name    = "${var.project_name}-shape-worker"
  role             = aws_iam_role.shape_worker.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 30
  filename         = data.external.shape_worker_zip.result.output_path
  source_code_hash = data.external.shape_worker_zip.result.sha256_base64
  depends_on       = [aws_cloudwatch_log_group.shape_worker]

  environment {
    variables = {
      DRAFTS_TABLE             = aws_dynamodb_table.drafts.name
      DRAFT_TTL_HOURS          = tostring(var.draft_ttl_hours)
      GITHUB_OWNER             = var.github_owner
      BRAINDUMP_REPO           = var.braindump_repo
      ANTHROPIC_MODEL          = var.anthropic_model
      TELEGRAM_BOT_TOKEN_PARAM = local.ssm.telegram_bot_token
      ANTHROPIC_API_KEY_PARAM  = local.ssm.anthropic_api_key
      GITHUB_PAT_PARAM         = local.ssm.github_pat

      # Workload identity federation -- see the comment in variables.tf.
      # Falls back to ANTHROPIC_API_KEY_PARAM above if unset.
      ANTHROPIC_FEDERATION_RULE_ID = var.anthropic_federation_rule_id
      ANTHROPIC_ORGANIZATION_ID    = var.anthropic_organization_id
      ANTHROPIC_SERVICE_ACCOUNT_ID = var.anthropic_service_account_id
      ANTHROPIC_WORKSPACE_ID       = var.anthropic_workspace_id
    }
  }
}

resource "aws_lambda_event_source_mapping" "shape_worker" {
  event_source_arn = aws_sqs_queue.shape_queue.arn
  function_name    = aws_lambda_function.shape_worker.arn
  batch_size       = 1
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.webhook_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

# Outstanding-work digest. Fired daily by EventBridge Scheduler, or on demand
# when webhook_handler async-invokes it for a /digest command.
resource "aws_lambda_function" "digest" {
  function_name    = "${var.project_name}-digest"
  role             = aws_iam_role.digest.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 120 # several GitHub pages + one LLM call
  filename         = data.external.digest_zip.result.output_path
  source_code_hash = data.external.digest_zip.result.sha256_base64
  depends_on       = [aws_cloudwatch_log_group.digest]

  environment {
    variables = {
      GITHUB_OWNER             = var.github_owner
      BRAINDUMP_REPO           = var.braindump_repo
      ANTHROPIC_MODEL          = var.anthropic_model
      ALLOWED_USER_ID          = tostring(var.allowed_telegram_user_id)
      TELEGRAM_BOT_TOKEN_PARAM = local.ssm.telegram_bot_token
      ANTHROPIC_API_KEY_PARAM  = local.ssm.anthropic_api_key
      GITHUB_PAT_PARAM         = local.ssm.github_pat

      # Workload identity federation -- see the comment in variables.tf.
      # Falls back to ANTHROPIC_API_KEY_PARAM above if unset.
      ANTHROPIC_FEDERATION_RULE_ID = var.anthropic_federation_rule_id
      ANTHROPIC_ORGANIZATION_ID    = var.anthropic_organization_id
      ANTHROPIC_SERVICE_ACCOUNT_ID = var.anthropic_service_account_id
      ANTHROPIC_WORKSPACE_ID       = var.anthropic_workspace_id
    }
  }
}
