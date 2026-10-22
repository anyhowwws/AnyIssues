terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }

  # No backend block: state is local by default. For remote S3 state (needed
  # by the GitHub Actions deploy), copy backend.tf.example to backend.tf.
}

provider "aws" {
  region = var.aws_region
}

provider "github" {
  token = var.github_pat
  owner = var.github_owner
}
