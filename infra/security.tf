# ---------------------------------------------------------------- KMS
# One customer-managed key for data at rest: RDS storage, the DB secret,
# uploaded assignment files and backups. Rotated automatically every year.
resource "aws_kms_key" "main" {
  description             = "${local.name} data-at-rest key (RDS, S3 uploads, secrets, backups)"
  enable_key_rotation     = true
  deletion_window_in_days = 7
}

resource "aws_kms_alias" "main" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.main.key_id
}

# ---------------------------------------------------------------- security groups
# Chain of trust: Lambda -> RDS Proxy -> RDS. Nothing else can reach the database.

resource "aws_security_group" "lambda" {
  name        = "${local.name}-lambda"
  description = "Lambda functions in private app subnets"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-lambda-sg" }
}

resource "aws_security_group" "proxy" {
  name        = "${local.name}-rds-proxy"
  description = "RDS Proxy"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-rds-proxy-sg" }
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "RDS PostgreSQL"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-db-sg" }
}

resource "aws_vpc_security_group_egress_rule" "lambda_https" {
  security_group_id = aws_security_group.lambda.id
  description       = "HTTPS to AWS APIs (SQS, S3, Secrets Manager, Cognito, X-Ray)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "lambda_to_proxy" {
  security_group_id            = aws_security_group.lambda.id
  description                  = "PostgreSQL to RDS Proxy"
  referenced_security_group_id = aws_security_group.proxy.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "proxy_from_lambda" {
  security_group_id            = aws_security_group.proxy.id
  description                  = "PostgreSQL from Lambda"
  referenced_security_group_id = aws_security_group.lambda.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "proxy_to_db" {
  security_group_id            = aws_security_group.proxy.id
  description                  = "PostgreSQL to the database"
  referenced_security_group_id = aws_security_group.db.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "proxy_https" {
  security_group_id = aws_security_group.proxy.id
  description       = "HTTPS to Secrets Manager for credentials"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "db_from_proxy" {
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from RDS Proxy only"
  referenced_security_group_id = aws_security_group.proxy.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}
