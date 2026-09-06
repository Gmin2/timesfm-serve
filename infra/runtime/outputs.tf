output "deployment" {
  description = "Non-secret configuration consumed by the offline Kubernetes renderer."
  value = {
    region                  = var.region
    cluster_name            = aws_eks_cluster.this.name
    artifact_bucket         = aws_s3_bucket.artifacts.id
    database_host           = aws_db_instance.this.address
    database_name           = aws_db_instance.this.db_name
    admin_secret_arn        = aws_db_instance.this.master_user_secret[0].secret_arn
    runtime_secrets         = { for k, v in aws_secretsmanager_secret.runtime : k => v.arn }
    repositories            = { for k, v in aws_ecr_repository.images : k => v.repository_url }
    alb_dns_name            = aws_lb.api.dns_name
    api_url                 = aws_apigatewayv2_api.weather.api_endpoint
    budget_email_configured = var.budget_email != null
    gpu_nodes               = var.gpu_nodes
    learning_enabled        = var.enable_learning
    database_cidrs          = aws_subnet.private[*].cidr_block
    s3_cidrs                = aws_vpc_endpoint.s3.cidr_blocks
    secrets_endpoint_cidrs  = [for eni in data.aws_network_interface.secrets : "${eni.private_ip}/32"]
  }
}
