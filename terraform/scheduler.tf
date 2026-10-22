# Daily outstanding-work digest. Change the time via var.digest_schedule /
# var.digest_timezone; no code change needed.
resource "aws_scheduler_schedule" "digest" {
  name                         = "${var.project_name}-digest"
  description                  = "Send the AnyIssues outstanding-work digest to Telegram"
  schedule_expression          = var.digest_schedule
  schedule_expression_timezone = var.digest_timezone
  state                        = var.digest_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.digest.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "schedule" })

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }
  }
}
