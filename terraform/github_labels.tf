# The bot's project-picker buttons are populated at runtime from the same
# query this data source runs: every non-fork, non-archived repo owned by
# var.github_owner. Creating a repo makes it a project automatically -- no
# deploy needed for the bot. This resource just keeps labels in sync on
# whatever that query currently returns (plus the braindump repo), so re-run
# `terraform apply` after creating a repo to give it the labels. Labels the
# repo already has (GitHub's default "bug") are adopted, not re-created.
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

# Labels that already exist on a repo -- most often GitHub's default "bug",
# which every new repo is created with -- can't be created again (422
# already_exists), and the provider won't adopt them on its own. Read what
# each repo has, and import any of ours that are present, so a new repo just
# needs the next apply. Import blocks are idempotent: once a label is in
# state, Terraform skips its import.
data "github_issue_labels" "existing" {
  for_each   = toset(local.label_repos)
  repository = each.value
}

locals {
  labels_to_adopt = {
    for key, pair in local.repo_label_pairs : key => pair
    if contains([for l in data.github_issue_labels.existing[pair.repo].labels : l.name], pair.label)
  }
}

import {
  for_each = local.labels_to_adopt
  to       = github_issue_label.this[each.key]
  id       = "${each.value.repo}:${each.value.label}"
}

resource "github_issue_label" "this" {
  for_each   = local.repo_label_pairs
  repository = each.value.repo
  name       = each.value.label
  color      = each.value.color
}
