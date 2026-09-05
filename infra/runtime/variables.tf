variable "account_id" {
  type        = string
  description = "Verified AWS account ID; the provider refuses credentials for another account."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the verified 12-digit AWS account ID."
  }
}

variable "deployment_approved" {
  type        = bool
  default     = false
  description = "Explicit cost approval gate. Keep false until a reviewed plan and budget are approved."
}

variable "region" {
  type    = string
  default = "us-east-1"
  validation {
    condition     = var.region == "us-east-1"
    error_message = "This pilot and its cost estimate are verified only for us-east-1."
  }
}

variable "name" {
  type    = string
  default = "pravah-weather-pilot"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,22}[a-z0-9]$", var.name)) && !strcontains(var.name, "--")
    error_message = "Use 3-24 lowercase letters, digits or single hyphens, starting with a letter and ending with a letter or digit."
  }
}

variable "operator_role_arn" {
  type        = string
  description = "Existing IAM role to grant EKS administration; never a root ARN or an STS session ARN."
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:role/.+", var.operator_role_arn))
    error_message = "Supply an existing IAM role ARN."
  }
}

variable "operator_cidrs" {
  type        = list(string)
  description = "Trusted operator public IPv4 ranges for the EKS API. Not the customer API."
  validation {
    condition     = length(var.operator_cidrs) > 0 && alltrue([for c in var.operator_cidrs : can(cidrnetmask(c)) && try(tonumber(split("/", c)[1]) >= 24, false)])
    error_message = "Use explicit IPv4 CIDRs no broader than /24; never 0.0.0.0/0."
  }
}

variable "budget_email" {
  type        = string
  default     = null
  description = "Optional alert recipient. Null records the budget without email notifications."
  validation {
    condition     = var.budget_email == null || can(regex("^[^@ ]+@[^@ ]+\\.[^@ ]+$", var.budget_email))
    error_message = "Supply an email address for budget notifications."
  }
}

variable "monthly_budget_usd" {
  type    = number
  default = 300
  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "Budget must be positive. Alerts are not a spending cap."
  }
}

variable "gpu_nodes" {
  type        = number
  default     = 0
  description = "0 by default. Set to 1 only for an approved CUDA test or demonstration. No autoscaler is installed."
  validation {
    condition     = contains([0, 1], var.gpu_nodes)
    error_message = "The pilot permits zero or one GPU node."
  }
}

variable "protect_data" {
  type        = bool
  default     = true
  description = "RDS/ALB deletion protection. Disable only for an approved teardown after export/backup."
}
