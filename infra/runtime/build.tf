variable "build_source_key" {
  type        = string
  default     = null
  description = "Content-addressed, allowlisted source ZIP. Null disables the manually started image builder."
  validation {
    condition     = var.build_source_key == null || can(regex("^builds/[a-f0-9]{64}\\.zip$", var.build_source_key))
    error_message = "Use builds/<SHA256>.zip."
  }
}

resource "aws_cloudwatch_log_group" "build" {
  count             = var.build_source_key == null ? 0 : 1
  name              = "/aws/codebuild/${var.name}-images"
  retention_in_days = 7
}

resource "aws_iam_role" "build" {
  count = var.build_source_key == null ? 0 : 1
  name  = "${var.name}-images"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.account_id }, ArnEquals = {
        "aws:SourceArn" = "arn:aws:codebuild:${var.region}:${var.account_id}:project/${var.name}-images"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "build" {
  count = var.build_source_key == null ? 0 : 1
  role  = aws_iam_role.build[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.build[0].arn}:*" },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.artifacts.arn}/${var.build_source_key}" },
      { Effect = "Allow", Action = ["s3:GetBucketLocation", "s3:GetBucketAcl"], Resource = aws_s3_bucket.artifacts.arn },
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      {
        Effect   = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages"]
        Resource = [for repository in aws_ecr_repository.images : repository.arn]
      }
    ]
  })
}

resource "aws_codebuild_project" "images" {
  count                  = var.build_source_key == null ? 0 : 1
  name                   = "${var.name}-images"
  service_role           = aws_iam_role.build[0].arn
  build_timeout          = 30
  queued_timeout         = 10
  concurrent_build_limit = 1
  artifacts { type = "NO_ARTIFACTS" }
  environment {
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    type                        = "LINUX_CONTAINER"
    image                       = "aws/codebuild/standard:7.0"
    image_pull_credentials_type = "CODEBUILD"
    privileged_mode             = true
    environment_variable {
      name  = "AWS_ACCOUNT_ID"
      value = var.account_id
    }
    environment_variable {
      name  = "PROJECT_NAME"
      value = var.name
    }
    environment_variable {
      name  = "RELEASE_TAG"
      value = trimsuffix(trimprefix(var.build_source_key, "builds/"), ".zip")
    }
  }
  source {
    type     = "S3"
    location = "${aws_s3_bucket.artifacts.id}/${var.build_source_key}"
    buildspec = yamlencode({
      version = 0.2
      phases  = { build = { commands = ["bash scripts/weather_build_images.sh"] } }
    })
  }
  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.build[0].name
      status     = "ENABLED"
    }
  }
  depends_on = [aws_iam_role_policy.build]
}

output "image_builder" {
  value = var.build_source_key == null ? null : {
    project_name = aws_codebuild_project.images[0].name
    source_key   = var.build_source_key
    release_tag  = trimsuffix(trimprefix(var.build_source_key, "builds/"), ".zip")
  }
}
