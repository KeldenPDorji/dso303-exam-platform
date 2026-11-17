output "site_url" {
  description = "Open this in a browser."
  value       = local.site_url
}

output "aws_region" {
  value = var.aws_region
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.main.id
}

output "frontend_bucket" {
  value = aws_s3_bucket.frontend.bucket
}

output "uploads_bucket" {
  value = aws_s3_bucket.uploads.bucket
}

output "user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "user_pool_client_id" {
  value = aws_cognito_user_pool_client.web.id
}

output "cognito_domain" {
  value = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.aws_region}.amazoncognito.com"
}

output "api_endpoint" {
  description = "Direct API Gateway URL (blocked by the origin check; use site_url/api instead)."
  value       = aws_apigatewayv2_api.http.api_endpoint
}

output "db_instance_id" {
  value = aws_db_instance.main.identifier
}

output "db_endpoint" {
  description = "Endpoint Lambda connects to (RDS Proxy, or the database when enable_rds_proxy = false)."
  value       = local.db_endpoint
}

output "lambda_functions" {
  value = { for k, f in aws_lambda_function.fn : k => f.function_name }
}

output "app_subnets" {
  description = "Private app subnet per AZ (used by the AZ-failure simulation)."
  value       = { for s in aws_subnet.app : s.availability_zone => s.id }
}

output "data_subnet_cidrs" {
  description = "Private data subnet CIDR per AZ: tells you which AZ the database primary is in."
  value       = { for s in aws_subnet.data : s.availability_zone => s.cidr_block }
}

output "lambda_security_group_id" {
  value = aws_security_group.lambda.id
}

output "max_upload_bytes" {
  value = local.max_upload_bytes
}
