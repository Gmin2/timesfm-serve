mock_provider "aws" {
  mock_resource "aws_s3_bucket" {
    override_during = plan
    defaults = { arn = "arn:aws:s3:::example-artifacts" }
  }
  mock_resource "aws_vpc_endpoint" {
    defaults = { network_interface_ids = ["eni-example-a", "eni-example-b"], cidr_blocks = ["198.51.100.0/24"] }
  }
  mock_data "aws_network_interface" {
    defaults = { private_ip = "10.42.10.10" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012", arn = "arn:aws:iam::123456789012:role/test-operator", user_id = "test" }
  }
}

variables {
  account_id          = "123456789012"
  deployment_approved = true
  operator_role_arn   = "arn:aws:iam::123456789012:role/test-operator"
  operator_cidrs      = ["203.0.113.1/32"]
  budget_email        = "operator@example.com"
}

run "pilot_safety" {
  command = plan
  assert {
    condition     = aws_eks_node_group.gpu.scaling_config[0].desired_size == 0 && aws_eks_node_group.gpu.scaling_config[0].max_size == 1
    error_message = "GPU capacity must default to zero and be capped at one."
  }
  assert {
    condition     = !aws_db_instance.this.publicly_accessible && aws_db_instance.this.storage_encrypted && aws_db_instance.this.deletion_protection && !aws_db_instance.this.skip_final_snapshot
    error_message = "Database must be private, encrypted and protected from accidental deletion."
  }
  assert {
    condition     = aws_db_instance.this.manage_master_user_password && length(aws_secretsmanager_secret.runtime) == 3
    error_message = "Credentials must be outside Terraform state and separated by workload."
  }
  assert {
    condition     = aws_eks_cluster.this.vpc_config[0].endpoint_private_access && aws_eks_cluster.this.vpc_config[0].public_access_cidrs == toset(["203.0.113.1/32"])
    error_message = "Kubernetes management access must be restricted."
  }
  assert {
    condition     = aws_lb.api.internal && aws_apigatewayv2_api.weather.protocol_type == "HTTP" && aws_apigatewayv2_integration.weather.connection_type == "VPC_LINK"
    error_message = "Public HTTPS must use API Gateway with a private ALB integration."
  }
  assert {
    condition     = !aws_s3_bucket.artifacts.force_destroy && alltrue([for r in aws_ecr_repository.images : !r.force_delete && r.image_tag_mutability == "IMMUTABLE"])
    error_message = "Artifact deletion and mutable image tags must not be implicit."
  }
  assert {
    condition     = aws_vpc_endpoint.secrets.private_dns_enabled && aws_vpc_endpoint.secrets.vpc_endpoint_type == "Interface" && jsondecode(aws_vpc_endpoint.s3.policy).Statement[0].Action == ["s3:GetObject"]
    error_message = "Secret access must be private and S3 egress must not permit writes."
  }
  assert {
    condition     = aws_apigatewayv2_integration.weather.request_parameters["overwrite:header.x-weather-client-ip"] == "$context.identity.sourceIp" && aws_wafv2_web_acl.api.scope == "REGIONAL" && !aws_wafv2_web_acl.api.visibility_config[0].sampled_requests_enabled
    error_message = "WAF must use a gateway-overwritten client identity without sampling credentials."
  }
}

run "no_invented_email" {
  command = plan
  variables { budget_email = null }
  assert {
    condition     = length(aws_budgets_budget.pilot.notification) == 0
    error_message = "Never invent an alert recipient when none is configured."
  }
}

run "bounded_image_builder" {
  command = plan
  variables { build_source_key = "builds/0000000000000000000000000000000000000000000000000000000000000000.zip" }
  assert {
    condition     = length(aws_codebuild_project.images) == 1 && aws_codebuild_project.images[0].concurrent_build_limit == 1 && aws_codebuild_project.images[0].build_timeout == 30
    error_message = "The manually started builder must have bounded concurrency and duration."
  }
}

run "no_approval" {
  command = plan
  variables { deployment_approved = false }
  expect_failures = [aws_vpc.this]
}

run "reject_gpu_expansion" {
  command = plan
  variables { gpu_nodes = 2 }
  expect_failures = [var.gpu_nodes]
}

run "reject_public_management" {
  command = plan
  variables { operator_cidrs = ["0.0.0.0/0"] }
  expect_failures = [var.operator_cidrs]
}
