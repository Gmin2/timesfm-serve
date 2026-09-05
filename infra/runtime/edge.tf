resource "aws_security_group" "alb" {
  name_prefix = "${var.name}-alb-"
  description = "Private API Gateway ingress, private NodePort targets"
  vpc_id      = aws_vpc.this.id
}

resource "aws_security_group" "vpc_link" {
  name_prefix = "${var.name}-vpc-link-"
  description = "API Gateway private integration"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_egress_rule" "vpc_link" {
  security_group_id            = aws_security_group.vpc_link.id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 80
  to_port                      = 80
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "gateway" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.vpc_link.id
  from_port                    = 80
  to_port                      = 80
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  from_port                    = 30080
  to_port                      = 30080
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "nodeport" {
  security_group_id            = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 30080
  to_port                      = 30080
  ip_protocol                  = "tcp"
}

resource "aws_lb" "api" {
  name                       = var.name
  load_balancer_type         = "application"
  internal                   = true
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.private[*].id
  enable_deletion_protection = var.protect_data
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "api" {
  name                 = var.name
  port                 = 30080
  protocol             = "HTTP"
  target_type          = "instance"
  vpc_id               = aws_vpc.this.id
  deregistration_delay = 30
  health_check {
    path                = "/health/ready"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }
}

# Terraform owns the ALB. No in-cluster load-balancer controller or broad controller IAM policy.
resource "aws_autoscaling_attachment" "api" {
  autoscaling_group_name = aws_eks_node_group.standard.resources[0].autoscaling_groups[0].name
  lb_target_group_arn    = aws_lb_target_group.api.arn
}

resource "aws_lb_listener" "private" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_apigatewayv2_api" "weather" {
  name                         = var.name
  protocol_type                = "HTTP"
  disable_execute_api_endpoint = false
}

resource "aws_apigatewayv2_vpc_link" "weather" {
  name               = var.name
  security_group_ids = [aws_security_group.vpc_link.id]
  subnet_ids         = aws_subnet.private[*].id
}

resource "aws_apigatewayv2_integration" "weather" {
  api_id                 = aws_apigatewayv2_api.weather.id
  integration_type       = "HTTP_PROXY"
  integration_method     = "ANY"
  integration_uri        = aws_lb_listener.private.arn
  connection_type        = "VPC_LINK"
  connection_id          = aws_apigatewayv2_vpc_link.weather.id
  payload_format_version = "1.0"
  timeout_milliseconds   = 29000
  request_parameters     = { "overwrite:path" = "$request.path" }
}

resource "aws_apigatewayv2_route" "weather" {
  api_id    = aws_apigatewayv2_api.weather.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.weather.id}"
}

resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/aws/apigateway/${var.name}"
  retention_in_days = 14
}

resource "aws_apigatewayv2_stage" "weather" {
  api_id      = aws_apigatewayv2_api.weather.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 40
    throttling_rate_limit  = 20
  }
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.gateway.arn
    format = jsonencode({
      requestId = "$context.requestId", routeKey = "$context.routeKey",
      status    = "$context.status", integrationError = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_budgets_budget" "pilot" {
  name         = var.name
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  # Account-wide on purpose: tag activation delays must not hide new pilot charges.
  dynamic "notification" {
    for_each = var.budget_email == null ? {} : { ACTUAL = 80, FORECASTED = 100 }
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.key
      subscriber_email_addresses = [var.budget_email]
    }
  }
}
