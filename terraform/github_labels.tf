# The bot's project-picker buttons are populated at runtime from the same
# query this data source runs: every non-fork, non-archived repo owned by
# var.github_owner. Creating a repo makes it a project automatically -- no
# deploy needed for the bot. This resource just keeps labels in sync on
# whatever that query currently returns (plus the braindump repo), so re-run
# `terraform apply` after creating a repo to give it the labels.
data "github_repositories" "tracked" {
  query = "user:${var.github_owner} archived:false fork:false"
}

locals {
  label_repos = distinct(concat(data.github_repositories.tracked.names, [var.braindump_repo]))

  labels = {
    "bug"                  = "d73a4a"
    "feature"              = "a2eeef"
    "eureka"               = "7057ff"
    "urgency:asap"         = "e11d21"
    "urgency:normal"       = "fbca04"
    "urgency:good-to-have" = "c5def5"
  }

  repo_label_pairs = {
    for pair in setproduct(local.label_repos, keys(local.labels)) :
    "${pair[0]}::${pair[1]}" => {
      repo  = pair[0]
      label = pair[1]
      color = local.labels[pair[1]]
    }
  }
}

resource "github_issue_label" "this" {
  for_each   = local.repo_label_pairs
  repository = each.value.repo
  name       = each.value.label
  color      = each.value.color
}
