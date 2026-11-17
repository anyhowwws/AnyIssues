# Daily outstanding-work digest. Change the time via var.digest_schedule /
# var.digest_timezone; no code change needed. The Lambda decides what to send:
# the full list on Mondays, ASAP + normal only on other days, and nothing at
# all when that leaves nothing to report.
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
    # <aws.scheduler.scheduled-time> is filled in by Scheduler, so a retry that
    # lands after midnight still counts as the day it was scheduled for. A plain
    # string, not jsonencode(): that escapes < and > and Scheduler would no
    # longer see the placeholder.
    input = "{\"source\": \"schedule\", \"scheduled_time\": \"<aws.scheduler.scheduled-time>\"}"

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }
  }
}
