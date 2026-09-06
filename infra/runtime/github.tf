variable "enable_github_oauth" {
  type        = bool
  default     = false
  description = "Provision the API-only GitHub OAuth secret container. Its value is set outside Terraform."
}

resource "aws_secretsmanager_secret" "github_oauth" {
  count                   = var.enable_github_oauth ? 1 : 0
  name                    = "${var.name}/github/oauth"
  recovery_window_in_days = 7
}

resource "aws_iam_role_policy" "github_oauth" {
  count = var.enable_github_oauth ? 1 : 0
  name  = "github-oauth"
  role  = aws_iam_role.workloads["api"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = aws_secretsmanager_secret.github_oauth[0].arn
    }]
  })
}

output "github_oauth_secret_arn" {
  value = try(aws_secretsmanager_secret.github_oauth[0].arn, null)
}
