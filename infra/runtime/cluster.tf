resource "aws_iam_role" "cluster" {
  name = "${var.name}-cluster"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "eks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = 14
}

resource "aws_eks_cluster" "this" {
  name                      = var.name
  role_arn                  = aws_iam_role.cluster.arn
  version                   = "1.35"
  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }
  upgrade_policy { support_type = "STANDARD" }
  vpc_config {
    subnet_ids              = aws_subnet.private[*].id
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.operator_cidrs
  }
  depends_on = [aws_iam_role_policy_attachment.cluster, aws_cloudwatch_log_group.cluster]
}

resource "aws_eks_access_entry" "operator" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = var.operator_role_arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "operator" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_eks_access_entry.operator.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}

resource "aws_iam_role" "nodes" {
  name = "${var.name}-nodes"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "nodes" {
  for_each   = toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly"])
  role       = aws_iam_role.nodes.name
  policy_arn = "arn:aws:iam::aws:policy/${each.value}"
}

resource "aws_launch_template" "nodes" {
  for_each               = { standard = 20, gpu = 80 }
  name_prefix            = "${var.name}-${each.key}-"
  update_default_version = true
  dynamic "credit_specification" {
    for_each = each.key == "standard" ? [1] : []
    content { cpu_credits = "standard" }
  }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      encrypted             = true
      volume_type           = "gp3"
      volume_size           = each.value
      delete_on_termination = true
    }
  }
  tag_specifications {
    resource_type = "instance"
    tags          = { Project = var.name, Name = "${var.name}-${each.key}" }
  }
}

resource "aws_eks_node_group" "standard" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "standard"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = aws_subnet.private[*].id
  instance_types  = ["t3.medium"]
  ami_type        = "AL2023_x86_64_STANDARD"
  release_version = "1.35.7-20260827"
  capacity_type   = "ON_DEMAND"
  labels          = { workload = "standard" }
  scaling_config {
    desired_size = 2
    min_size     = 2
    max_size     = 2
  }
  update_config { max_unavailable = 1 }
  launch_template {
    id      = aws_launch_template.nodes["standard"].id
    version = aws_launch_template.nodes["standard"].latest_version
  }
  depends_on = [aws_iam_role_policy_attachment.nodes, aws_eks_addon.cni, aws_route_table_association.private]
}

resource "aws_eks_node_group" "gpu" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "gpu"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = aws_subnet.private[*].id
  instance_types  = ["g6.xlarge"]
  ami_type        = "AL2023_x86_64_NVIDIA"
  release_version = "1.35.7-20260827"
  capacity_type   = "ON_DEMAND"
  labels          = { workload = "gpu" }
  taint {
    key    = "workload"
    value  = "gpu"
    effect = "NO_SCHEDULE"
  }
  scaling_config {
    desired_size = var.gpu_nodes
    min_size     = 0
    max_size     = 1
  }
  update_config {
    max_unavailable = 1
    update_strategy = "MINIMAL"
  }
  launch_template {
    id      = aws_launch_template.nodes["gpu"].id
    version = aws_launch_template.nodes["gpu"].latest_version
  }
  depends_on = [aws_iam_role_policy_attachment.nodes, aws_eks_addon.cni, aws_route_table_association.private]
}

resource "aws_eks_addon" "identity" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "eks-pod-identity-agent"
  addon_version               = "v1.3.10-eksbuild.3"
  resolve_conflicts_on_create = "OVERWRITE"
}

resource "aws_iam_role" "cni" {
  name               = "${var.name}-cni"
  assume_role_policy = local.pod_trust["cni"]
}

resource "aws_iam_role_policy_attachment" "cni" {
  role       = aws_iam_role.cni.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_eks_addon" "cni" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "vpc-cni"
  addon_version               = "v1.22.4-eksbuild.3"
  resolve_conflicts_on_create = "OVERWRITE"
  configuration_values        = jsonencode({ enableNetworkPolicy = "true" })
  pod_identity_association {
    role_arn        = aws_iam_role.cni.arn
    service_account = "aws-node"
  }
  depends_on = [aws_eks_addon.identity, aws_iam_role_policy_attachment.cni]
}

resource "aws_eks_addon" "core" {
  for_each                    = { coredns = "v1.13.2-eksbuild.21", kube-proxy = "v1.35.3-eksbuild.21" }
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.key
  addon_version               = each.value
  resolve_conflicts_on_create = "OVERWRITE"
  depends_on                  = [aws_eks_node_group.standard]
}
