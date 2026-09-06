resource "aws_wafv2_web_acl" "api" {
  name  = "${var.name}-api"
  scope = "REGIONAL"
  default_action {
    allow {}
  }

  custom_response_body {
    key          = "rate-limited"
    content_type = "APPLICATION_JSON"
    content      = jsonencode({ detail = "rate_limit_exceeded" })
  }

  rule {
    name     = "require-gateway-client-ip"
    priority = 0
    action {
      block {}
    }
    statement {
      size_constraint_statement {
        comparison_operator = "EQ"
        size                = 0
        field_to_match {
          single_header { name = "x-weather-client-ip" }
        }
        text_transformation {
          priority = 0
          type     = "NONE"
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-missing-client-ip"
      sampled_requests_enabled   = false
    }
  }

  rule {
    name     = "per-client-rate-limit"
    priority = 1
    action {
      block {
        custom_response {
          response_code            = 429
          custom_response_body_key = "rate-limited"
          response_header {
            name  = "retry-after"
            value = "60"
          }
        }
      }
    }
    statement {
      rate_based_statement {
        limit                 = 120
        evaluation_window_sec = 60
        aggregate_key_type    = "FORWARDED_IP"
        forwarded_ip_config {
          header_name       = "x-weather-client-ip"
          fallback_behavior = "MATCH"
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-client-rate"
      sampled_requests_enabled   = false
    }
  }

  rule {
    name     = "bounded-request-body"
    priority = 2
    action {
      block {
        custom_response { response_code = 413 }
      }
    }
    statement {
      size_constraint_statement {
        comparison_operator = "GT"
        size                = 8192
        field_to_match {
          body { oversize_handling = "MATCH" }
        }
        text_transformation {
          priority = 0
          type     = "NONE"
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-request-body"
      sampled_requests_enabled   = false
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.name}-api"
    sampled_requests_enabled   = false
  }
}

# The HTTP API uses a private ALB; WAF associates with that supported resource.
resource "aws_wafv2_web_acl_association" "api" {
  resource_arn = aws_lb.api.arn
  web_acl_arn  = aws_wafv2_web_acl.api.arn
  depends_on   = [aws_apigatewayv2_integration.weather]
}
