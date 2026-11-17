data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

resource "random_id" "suffix" {
  byte_length = 3
}

# Shared secret CloudFront adds to every /api request; the API Lambda rejects
# requests without it, so nobody can bypass CloudFront + WAF by calling API Gateway directly.
resource "random_password" "origin_verify" {
  length  = 40
  special = false
}

locals {
  name              = "${var.project}-${var.environment}"
  azs               = slice(data.aws_availability_zones.available.names, 0, 2)
  account_id        = data.aws_caller_identity.current.account_id
  suffix            = random_id.suffix.hex
  use_custom_domain = var.domain_name != "" && var.hosted_zone_id != ""
  site_url          = local.use_custom_domain ? "https://${var.domain_name}" : "https://${aws_cloudfront_distribution.main.domain_name}"
  max_upload_bytes  = var.max_upload_mb * 1024 * 1024
  ses_recipients    = compact([for e in split(",", var.ses_verified_recipients) : trimspace(e)])
}
