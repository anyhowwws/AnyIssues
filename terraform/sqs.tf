resource "aws_sqs_queue" "shape_dlq" {
  name                      = "${var.project_name}-shape-dlq"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_sqs_queue" "shape_queue" {
  name                       = "${var.project_name}-shape-queue"
  visibility_timeout_seconds = 60 # >= shape_worker Lambda timeout

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.shape_dlq.arn
    maxReceiveCount     = 3
  })
}
