locals {
  azs = ["us-east-1a", "us-east-1b"]
}

resource "aws_vpc" "this" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  lifecycle {
    precondition {
      condition     = var.deployment_approved
      error_message = "AWS creation is not approved. Review cost, identity, DNS, plan and teardown before setting deployment_approved=true."
    }
  }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.this.id
  availability_zone       = local.azs[count.index]
  cidr_block              = cidrsubnet(aws_vpc.this.cidr_block, 8, count.index)
  map_public_ip_on_launch = false
  tags                    = { "kubernetes.io/role/elb" = "1" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(aws_vpc.this.cidr_block, 8, count.index + 10)
  tags              = { "kubernetes.io/role/internal-elb" = "1" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
}

resource "aws_eip" "nat" {
  domain = "vpc"
}

# Deliberate pilot tradeoff: one NAT, not an AZ-resilient egress design.
resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this.id
  }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow", Principal = "*", Action = ["s3:GetObject"]
      Resource = ["${aws_s3_bucket.artifacts.arn}/*", "arn:aws:s3:::prod-${var.region}-starport-layer-bucket/*"]
    }]
  })
}

resource "aws_security_group" "secrets_endpoint" {
  name_prefix = "${var.name}-secrets-"
  description = "Private Secrets Manager access from project nodes"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "secrets_endpoint" {
  security_group_id            = aws_security_group.secrets_endpoint.id
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
}

resource "aws_vpc_endpoint" "secrets" {
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.secrets_endpoint.id]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = "*"
      Action = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret"]
      Resource = concat([aws_db_instance.this.master_user_secret[0].secret_arn], values(aws_secretsmanager_secret.runtime)[*].arn,
      aws_secretsmanager_secret.github_oauth[*].arn)
    }]
  })
}

data "aws_network_interface" "secrets" {
  count = 2
  id    = sort(tolist(aws_vpc_endpoint.secrets.network_interface_ids))[count.index]
}
