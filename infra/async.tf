# Asynchronous processing: SQS queues (each with a dead-letter queue) feeding
# Lambda workers, an EventBridge schedule for the exam sweeper, and SES for email.

locals {
  queues = {
    submissions   = { consumer = "processor", visibility = 360, max_concurrency = 10 }
    marking       = { consumer = "marker", visibility = 360, max_concurrency = 10 }
    notifications = { consumer = "notifier", visibility = 180, max_concurrency = 5 } # stays under SES send rate
  }
}

resource "aws_sqs_queue" "dlq" {
  for_each                  = local.queues
  name                      = "${local.name}-${each.key}-dlq"
  message_retention_seconds = 1209600 # 14 days to investigate and redrive
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "main" {
  for_each                   = local.queues
  name                       = "${local.name}-${each.key}"
  visibility_timeout_seconds = each.value.visibility # 6x the consumer's timeout (AWS guidance)
  message_retention_seconds  = 345600                # 4 days
  receive_wait_time_seconds  = 20                    # long polling
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = 5
  })
}

# Only our uploads bucket may publish to the submissions queue.
data "aws_iam_policy_document" "submissions_queue" {
  statement {
    sid       = "AllowUploadsBucketEvents"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.main["submissions"].arn]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.uploads.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "submissions" {
  queue_url = aws_sqs_queue.main["submissions"].id
  policy    = data.aws_iam_policy_document.submissions_queue.json
}

resource "aws_lambda_event_source_mapping" "workers" {
  for_each                           = local.queues
  event_source_arn                   = aws_sqs_queue.main[each.key].arn
  function_name                      = aws_lambda_function.fn[each.value.consumer].arn
  batch_size                         = 10
  maximum_batching_window_in_seconds = 1
  function_response_types            = ["ReportBatchItemFailures"] # retry only the failed messages

  scaling_config {
    maximum_concurrency = each.value.max_concurrency # protects the database and SES from bursts
  }
}

# ---------------------------------------------------------------- exam sweeper schedule

resource "aws_cloudwatch_event_rule" "sweeper" {
  name                = "${local.name}-exam-sweeper"
  description         = "Auto-submit expired exam attempts and re-queue unmarked ones"
  schedule_expression = "rate(2 minutes)"
}

resource "aws_cloudwatch_event_target" "sweeper" {
  rule  = aws_cloudwatch_event_rule.sweeper.name
  arn   = aws_lambda_function.fn["marker"].arn
  input = jsonencode({ sweep = true })
}

resource "aws_lambda_permission" "sweeper" {
  statement_id  = "AllowEventBridgeSweeper"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["marker"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.sweeper.arn
}

# ---------------------------------------------------------------- SES
# Each identity receives a verification email that must be clicked once.
# Production: verify the university domain (DKIM/SPF/DMARC) and request SES production access.

resource "aws_sesv2_email_identity" "sender" {
  email_identity = var.ses_sender_email
}

resource "aws_sesv2_email_identity" "recipients" {
  for_each       = toset([for e in local.ses_recipients : e if e != var.ses_sender_email])
  email_identity = each.value
}
