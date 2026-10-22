# Shared secret Telegram sends back on every webhook call (X-Telegram-Bot-Api-Secret-Token).
# Low-sensitivity compared to the bot token / PAT, so a plain Lambda env var is fine here.
resource "random_password" "webhook_secret" {
  length  = 32
  special = false
}
