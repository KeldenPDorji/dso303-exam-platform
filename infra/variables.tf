variable "aws_region" {
  description = "Primary region (choose the one closest to the university)."
  type        = string
  default     = "ap-southeast-2"
}

variable "project" {
  description = "Short name used as a prefix for every resource."
  type        = string
  default     = "exam-platform"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "prod"
}

variable "vpc_cidr" {
  description = "CIDR block of the VPC. Each AZ gets a public, private-app and private-data /24."
  type        = string
  default     = "10.20.0.0/16"
}

variable "single_nat_gateway" {
  description = "true = one NAT gateway (cheaper lab mode, NOT AZ-resilient). Keep false for the real design."
  type        = bool
  default     = false
}

variable "db_instance_class" {
  description = "RDS instance class. Production sizing for exam week: db.r6g.large. AWS Free plan accounts: db.t4g.micro only."
  type        = string
  default     = "db.t4g.small"
}

variable "enable_rds_proxy" {
  description = "Put RDS Proxy between Lambda and the database. AWS Free plan accounts cannot create RDS Proxy: set false there."
  type        = bool
  default     = true
}

variable "db_allocated_storage" {
  description = "Initial gp3 storage in GB (storage autoscaling is enabled up to 200 GB)."
  type        = number
  default     = 20
}

variable "db_backup_retention_days" {
  description = "RDS automated backup / point-in-time-restore window. Production: 35. AWS Free plan accounts: max 1."
  type        = number
  default     = 7
}

variable "db_deletion_protection" {
  description = "Protect the database from deletion. Production: true (set false only for a lab teardown)."
  type        = bool
  default     = false
}

variable "ses_sender_email" {
  description = "Verified SES sender address for receipts and notifications."
  type        = string
}

variable "ses_verified_recipients" {
  description = "Comma-separated emails to verify in SES (required while the account is in the SES sandbox)."
  type        = string
  default     = ""
}

variable "alarm_email" {
  description = "Email that receives CloudWatch alarm notifications (empty = no subscription)."
  type        = string
  default     = ""
}

variable "domain_name" {
  description = "Optional custom domain (e.g. exams.example.edu). Empty = use the CloudFront domain."
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Route 53 public hosted zone ID for domain_name (required when domain_name is set)."
  type        = string
  default     = ""
}

variable "api_provisioned_concurrency" {
  description = "Pre-warmed API Lambda environments. 0 normally; set e.g. 50 during exam week."
  type        = number
  default     = 0
}

variable "waf_rate_limit_per_ip" {
  description = "Requests per IP per 5 minutes before WAF blocks. Kept high because a whole exam hall can share one NAT IP."
  type        = number
  default     = 100000
}

variable "max_upload_mb" {
  description = "Largest assignment file accepted (enforced by the signed S3 POST policy)."
  type        = number
  default     = 50
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention."
  type        = number
  default     = 30
}

variable "enable_cloudtrail" {
  description = "Create an audit trail (management events + S3 data events on the uploads bucket)."
  type        = bool
  default     = true
}
