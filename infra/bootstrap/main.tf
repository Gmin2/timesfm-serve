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

variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the verified AWS account ID."
  }
}

variable "operator_principal_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:(user|role)/.+$", var.operator_principal_arn))
    error_message = "Use one trusted IAM user or role, never root or an STS session."
  }
}

variable "deployment_approved" {
  type    = bool
  default = false
}

locals {
  name = "pravah-weather-pilot"
}

provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = { Project = local.name, Environment = "pilot", ManagedBy = "terraform" }
  }
}

resource "aws_s3_bucket" "state" {
  bucket        = "pravah-weather-tfstate-${var.account_id}-us-east-1"
  force_destroy = false
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = var.deployment_approved && split(":", var.operator_principal_arn)[4] == var.account_id
      error_message = "Explicit deployment approval and a same-account operator are required."
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_iam_role" "operator" {
  name                 = "${local.name}-operator"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { AWS = var.operator_principal_arn }, Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "operator" {
  name = "DescribeWeatherCluster"
  role = aws_iam_role.operator.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow", Action = ["eks:DescribeCluster"]
      Resource = "arn:aws:eks:us-east-1:${var.account_id}:cluster/${local.name}"
    }]
  })
}

output "state_bucket" { value = aws_s3_bucket.state.id }
output "operator_role_arn" { value = aws_iam_role.operator.arn }
