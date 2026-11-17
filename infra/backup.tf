# AWS Backup: long-term database snapshots for the 5-year record-keeping requirement.
# (RDS automated backups only go back 35 days; they cover point-in-time restore.)

resource "aws_backup_vault" "main" {
  name          = "${local.name}-vault"
  kms_key_arn   = aws_kms_key.main.arn
  force_destroy = true # LAB ONLY so teardown works. Production: false + Vault Lock.
}

resource "aws_backup_plan" "main" {
  name = "${local.name}-records-5y"

  rule {
    rule_name         = "daily-35-days"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 17 * * ? *)" # 03:00 AEST
    lifecycle {
      delete_after = 35
    }
  }

  rule {
    rule_name         = "monthly-5-years"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 18 1 * ? *)"
    lifecycle {
      delete_after = 1830
    }
  }
}

data "aws_iam_policy_document" "backup_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["backup.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backup" {
  name               = "${local.name}-backup"
  assume_role_policy = data.aws_iam_policy_document.backup_assume.json
}

resource "aws_iam_role_policy_attachment" "backup" {
  for_each = toset([
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup",
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores",
  ])
  role       = aws_iam_role.backup.name
  policy_arn = each.value
}

resource "aws_backup_selection" "db" {
  name         = "${local.name}-db"
  plan_id      = aws_backup_plan.main.id
  iam_role_arn = aws_iam_role.backup.arn
  resources    = [aws_db_instance.main.arn]
}
