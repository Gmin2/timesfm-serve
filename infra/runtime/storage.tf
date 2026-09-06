resource "aws_s3_bucket" "artifacts" {
  bucket        = "${var.name}-${data.aws_caller_identity.current.account_id}"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "learning" {
  count  = var.enable_learning ? 1 : 0
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "expire-research-training-artifacts"
    status = "Enabled"
    filter { prefix = "training/" }
    expiration { days = 30 }
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*"
      Resource  = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_ecr_repository" "images" {
  for_each             = toset(["api", "worker", "ingest", "bootstrap"])
  name                 = "${var.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_security_group" "database" {
  name_prefix = "${var.name}-database-"
  description = "PostgreSQL only from EKS nodes; no public ingress"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "database" {
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_db_parameter_group" "this" {
  name_prefix = "${var.name}-"
  family      = "postgres16"
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }
}

resource "aws_db_instance" "this" {
  identifier                      = var.name
  engine                          = "postgres"
  engine_version                  = "16.15"
  instance_class                  = "db.t4g.micro"
  allocated_storage               = 20
  max_allocated_storage           = 30
  storage_type                    = "gp3"
  storage_encrypted               = true
  db_name                         = "weather"
  username                        = "weather_admin"
  manage_master_user_password     = true
  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.database.id]
  parameter_group_name            = aws_db_parameter_group.this.name
  publicly_accessible             = false
  multi_az                        = false
  backup_retention_period         = 7
  deletion_protection             = var.protect_data
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${var.name}-final"
  copy_tags_to_snapshot           = true
  auto_minor_version_upgrade      = true
  allow_major_version_upgrade     = false
  apply_immediately               = false
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
}

# Values are created by the operator migration job, never placed in Terraform state.
resource "aws_secretsmanager_secret" "runtime" {
  for_each                = toset(["api", "worker", "ingest"])
  name                    = "${var.name}/database/${each.key}"
  recovery_window_in_days = 7
}
