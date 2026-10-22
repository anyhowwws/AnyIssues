output "webhook_url" {
  description = "Set this as your Telegram bot's webhook (see README, \"Connect Telegram to your bot\"). Read with -raw."
  value       = "${aws_apigatewayv2_api.this.api_endpoint}/webhook"
  sensitive   = true
}

output "webhook_secret" {
  description = "Pass this as secret_token when calling setWebhook"
  value       = random_password.webhook_secret.result
  sensitive   = true
}

output "drafts_table_name" {
  value = aws_dynamodb_table.drafts.name
}

output "digest_function_name" {
  description = "Invoke manually with: aws lambda invoke --function-name <this> /dev/stdout"
  value       = aws_lambda_function.digest.function_name
}

# ---------------- Optional: keyless Claude auth (README, "Workload Identity Federation") ----------------
# Both Lambdas that call Claude share one federation rule but run as different
# IAM roles, so the rule must accept both. A single "Subject pattern" can only
# name one role; this CEL expression names exactly the two.

output "anthropic_wif_role_arns" {
  description = "IAM roles that exchange tokens with Anthropic. Your federation rule must accept both."
  value       = [aws_iam_role.shape_worker.arn, aws_iam_role.digest.arn]
}

output "anthropic_wif_cel_expression" {
  description = "Paste into Claude Console -> Workload identity -> your rule -> Match configuration -> CEL expression."
  value       = format("claims.sub in [%q, %q]", aws_iam_role.shape_worker.arn, aws_iam_role.digest.arn)
}
