# ================================================================ packaging
# backend/src -> one zip shared by every function (different handlers).
# backend/build/layer -> pg8000 dependency layer (created by scripts/build.sh).

data "archive_file" "app" {
  type        = "zip"
  source_dir  = "${path.module}/../backend/src"
  output_path = "${path.module}/.build/app.zip"
  excludes    = ["__pycache__", "common/__pycache__"]
}

data "archive_file" "deps" {
  type        = "zip"
  source_dir  = "${path.module}/../backend/build/layer"
  output_path = "${path.module}/.build/deps-layer.zip"
}

resource "aws_lambda_layer_version" "deps" {
  layer_name               = "${local.name}-deps"
  filename                 = data.archive_file.deps.output_path
  source_code_hash         = data.archive_file.deps.output_base64sha256
  compatible_runtimes      = ["python3.13"]
  compatible_architectures = ["arm64"]
}

# ================================================================ functions

locals {
  lambda_env_common = {
    DB_SECRET_ARN     = aws_db_instance.main.master_user_secret[0].secret_arn
    DB_HOST           = aws_db_proxy.main.endpoint
    DB_NAME           = aws_db_instance.main.db_name
    UPLOAD_BUCKET     = aws_s3_bucket.uploads.bucket
    MARKING_QUEUE_URL = aws_sqs_queue.main["marking"].url
    NOTIFY_QUEUE_URL  = aws_sqs_queue.main["notifications"].url
    MAX_UPLOAD_BYTES  = tostring(local.max_upload_bytes)
    SITE_URL          = local.site_url
    LOG_LEVEL         = "INFO"
  }

  lambda_functions = {
    api = {
      description = "REST API: exams, autosave, assignments, admin"
      handler     = "api.handler"
      timeout     = 15
      memory      = 512
      in_vpc      = true
      env = {
        USER_POOL_ID         = aws_cognito_user_pool.main.id
        ORIGIN_VERIFY_SECRET = random_password.origin_verify.result
      }
    }
    processor = {
      description = "Validates uploaded files and records submissions (S3 -> SQS)"
      handler     = "submission_processor.handler"
      timeout     = 60
      memory      = 256
      in_vpc      = true
      env         = {}
    }
    marker = {
      description = "Auto-marks MCQs; scheduled sweeper auto-submits expired attempts"
      handler     = "marker.handler"
      timeout     = 60
      memory      = 256
      in_vpc      = true
      env         = {}
    }
    notifier = {
      description = "Sends emails with SES (runs outside the VPC: no private dependencies)"
      handler     = "notifier.handler"
      timeout     = 30
      memory      = 256
      in_vpc      = false
      env         = { SES_FROM = var.ses_sender_email }
    }
    migrate = {
      description = "Applies the database schema; invoked by the CI/CD pipeline"
      handler     = "migrate.handler"
      timeout     = 120
      memory      = 256
      in_vpc      = true
      env         = {}
    }
  }
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = local.lambda_functions
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "fn" {
  for_each = local.lambda_functions

  function_name    = "${local.name}-${each.key}"
  description      = each.value.description
  role             = aws_iam_role.lambda[each.key].arn
  runtime          = "python3.13"
  architectures    = ["arm64"] # Graviton: ~20% cheaper per GB-second
  handler          = each.value.handler
  filename         = data.archive_file.app.output_path
  source_code_hash = data.archive_file.app.output_base64sha256
  layers           = [aws_lambda_layer_version.deps.arn]
  timeout          = each.value.timeout
  memory_size      = each.value.memory
  publish          = each.key == "api" # versions for the "live" alias / provisioned concurrency

  dynamic "vpc_config" {
    for_each = each.value.in_vpc ? [1] : []
    content {
      subnet_ids         = aws_subnet.app[*].id # both AZs: Lambda keeps running if one AZ fails
      security_group_ids = [aws_security_group.lambda.id]
    }
  }

  tracing_config {
    mode = "Active" # AWS X-Ray
  }

  environment {
    variables = merge(local.lambda_env_common, each.value.env)
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy.lambda,
    aws_iam_role_policy_attachment.lambda,
  ]
}

resource "aws_lambda_alias" "api_live" {
  name             = "live"
  function_name    = aws_lambda_function.fn["api"].function_name
  function_version = aws_lambda_function.fn["api"].version
}

# Exam week: pre-initialised environments so the first of 3,000 students never waits for a cold start.
resource "aws_lambda_provisioned_concurrency_config" "api" {
  count                             = var.api_provisioned_concurrency > 0 ? 1 : 0
  function_name                     = aws_lambda_function.fn["api"].function_name
  qualifier                         = aws_lambda_alias.api_live.name
  provisioned_concurrent_executions = var.api_provisioned_concurrency
}

# ================================================================ IAM (one least-privilege role per function)

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  for_each           = local.lambda_functions
  name               = "${local.name}-${each.key}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

locals {
  lambda_managed_policies = merge([
    for fn, cfg in local.lambda_functions : {
      "${fn}-base" = {
        fn  = fn
        arn = cfg.in_vpc ? "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole" : "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
      }
      "${fn}-xray" = { fn = fn, arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess" }
    }
  ]...)
}

resource "aws_iam_role_policy_attachment" "lambda" {
  for_each   = local.lambda_managed_policies
  role       = aws_iam_role.lambda[each.value.fn].name
  policy_arn = each.value.arn
}

data "aws_iam_policy_document" "db_access" {
  statement {
    sid       = "ReadDatabaseSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_db_instance.main.master_user_secret[0].secret_arn]
  }
  statement {
    sid       = "DecryptWithPlatformKey"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
}

data "aws_iam_policy_document" "api" {
  source_policy_documents = [data.aws_iam_policy_document.db_access.json]

  statement {
    sid       = "PresignSubmissionUploadAndDownload"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.uploads.arn}/submissions/*"]
  }
  statement {
    sid       = "EncryptUploads"
    actions   = ["kms:GenerateDataKey"]
    resources = [aws_kms_key.main.arn]
  }
  statement {
    sid       = "QueueMarkingAndEmails"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.main["marking"].arn, aws_sqs_queue.main["notifications"].arn]
  }
  statement {
    sid       = "ManageUsers"
    actions   = ["cognito-idp:AdminCreateUser", "cognito-idp:AdminAddUserToGroup", "cognito-idp:ListUsersInGroup"]
    resources = [aws_cognito_user_pool.main.arn]
  }
}

data "aws_iam_policy_document" "processor" {
  source_policy_documents = [data.aws_iam_policy_document.db_access.json]

  statement {
    sid       = "ReadSubmissions"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.uploads.arn}/submissions/*"]
  }
  statement {
    sid       = "ConsumeSubmissionsQueue"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
    resources = [aws_sqs_queue.main["submissions"].arn]
  }
  statement {
    sid       = "QueueEmails"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.main["notifications"].arn]
  }
}

data "aws_iam_policy_document" "marker" {
  source_policy_documents = [data.aws_iam_policy_document.db_access.json]

  statement {
    sid       = "ConsumeAndRequeueMarking"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility", "sqs:SendMessage"]
    resources = [aws_sqs_queue.main["marking"].arn]
  }
}

data "aws_iam_policy_document" "notifier" {
  statement {
    sid       = "ConsumeNotificationsQueue"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
    resources = [aws_sqs_queue.main["notifications"].arn]
  }
  statement {
    sid       = "SendEmail"
    actions   = ["ses:SendEmail"]
    resources = ["arn:aws:ses:${var.aws_region}:${local.account_id}:identity/*"]
  }
}

locals {
  lambda_policies = {
    api       = data.aws_iam_policy_document.api.json
    processor = data.aws_iam_policy_document.processor.json
    marker    = data.aws_iam_policy_document.marker.json
    notifier  = data.aws_iam_policy_document.notifier.json
    migrate   = data.aws_iam_policy_document.db_access.json
  }
}

resource "aws_iam_role_policy" "lambda" {
  for_each = local.lambda_policies
  name     = "least-privilege"
  role     = aws_iam_role.lambda[each.key].id
  policy   = each.value
}
