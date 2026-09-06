locals {
  pod_scopes = merge({ for name in ["api", "worker", "ingest", "migrate"] : name => { namespace = "weather", service_account = "weather-${name}" } }, {
    cni = { namespace = "kube-system", service_account = "aws-node" }
  })
  pod_trust = { for name, scope in local.pod_scopes : name => jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Service = "pods.eks.amazonaws.com" }
      Action = ["sts:AssumeRole", "sts:TagSession"]
      Condition = { StringEquals = {
        "aws:RequestTag/eks-cluster-arn"            = aws_eks_cluster.this.arn
        "aws:RequestTag/kubernetes-namespace"       = scope.namespace
        "aws:RequestTag/kubernetes-service-account" = scope.service_account
      } }
    }]
  }) }
}

resource "aws_iam_role" "workloads" {
  for_each           = toset(["api", "worker", "ingest", "migrate"])
  name               = "${var.name}-${each.key}"
  assume_role_policy = local.pod_trust[each.key]
}

resource "aws_iam_role_policy" "runtime" {
  for_each = toset(["api", "worker", "ingest"])
  role     = aws_iam_role.workloads[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.runtime[each.key].arn }
      ], each.key == "ingest" ? [] : [{
        Effect   = "Allow", Action = ["s3:GetObject"]
        Resource = concat(["${aws_s3_bucket.artifacts.arn}/replays/*"], each.key == "worker" ? ["${aws_s3_bucket.artifacts.arn}/models/*"] : [])
    }])
  })
}

resource "aws_iam_role_policy" "learning" {
  count = var.enable_learning ? 1 : 0
  name  = "research-training-artifacts"
  role  = aws_iam_role.workloads["worker"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow", Action = ["s3:PutObject"]
      Resource = "${aws_s3_bucket.artifacts.arn}/training/*"
    }]
  })
}

resource "aws_iam_role_policy" "migrate" {
  role = aws_iam_role.workloads["migrate"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = concat([aws_db_instance.this.master_user_secret[0].secret_arn], values(aws_secretsmanager_secret.runtime)[*].arn) },
      { Effect = "Allow", Action = ["secretsmanager:PutSecretValue"], Resource = values(aws_secretsmanager_secret.runtime)[*].arn }
    ]
  })
}

resource "aws_eks_pod_identity_association" "workloads" {
  for_each        = aws_iam_role.workloads
  cluster_name    = aws_eks_cluster.this.name
  namespace       = "weather"
  service_account = "weather-${each.key}"
  role_arn        = each.value.arn
  depends_on      = [aws_eks_addon.identity]
}
