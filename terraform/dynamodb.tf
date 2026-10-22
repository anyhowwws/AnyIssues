# One item per idea, from the moment a message arrives until it's confirmed
# into a GitHub issue (or discarded, or left to expire via TTL).
#
# Item shape:
#   draft_id        (S)  8-char id, also embedded in every callback_data payload
#   chat_id          (N)
#   message_id       (N)  the bot's own preview message, edited in place as buttons are tapped
#   status           (S)  processing | pending | awaiting_edit | confirming | confirmed
#                         | discarded | failed
#   raw_text         (S)  the user's original message
#   llm_title        (S)
#   llm_body         (S)
#   repos_snapshot   (L)  repo names offered as buttons at shape time
#   project          (S)  selected repo name, or "eureka"
#   type             (S)  bug | feature | eureka
#   urgency          (S)  asap | normal | good-to-have
#   created_at       (N)  epoch seconds
#   ttl              (N)  epoch seconds, DynamoDB TTL attribute
#   issue_url        (S)  set once confirmed
#
# The table also holds idempotency markers written by webhook_handler:
#   draft_id = "upd:<telegram update_id>", status = "seen", ttl = +24h.
# A conditional put on that key is what drops Telegram's redeliveries.
resource "aws_dynamodb_table" "drafts" {
  name         = "${var.project_name}-drafts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "draft_id"

  attribute {
    name = "draft_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}
