terraform {
  required_version = "= 1.16.1"
  backend "s3" {}
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.63.0"
    }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = { Project = var.name, Environment = "pilot", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
