# ================================================================ frontend bucket
# Private bucket. Only CloudFront (via Origin Access Control) can read it.

resource "aws_s3_bucket" "frontend" {
  bucket        = "${local.name}-web-${local.suffix}"
  force_destroy = true # static build output only; safe to empty on teardown
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # public web assets: SSE-S3 avoids KMS calls on every CloudFront fetch
    }
  }
}

data "aws_iam_policy_document" "frontend" {
  statement {
    sid       = "AllowCloudFrontOAC"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.main.arn]
    }
  }

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.frontend.arn, "${aws_s3_bucket.frontend.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket     = aws_s3_bucket.frontend.id
  policy     = data.aws_iam_policy_document.frontend.json
  depends_on = [aws_s3_bucket_public_access_block.frontend]
}

# ================================================================ uploads bucket
# Assignment files. Browsers upload directly with a presigned POST, so 50 MB
# files never pass through API Gateway (10 MB limit) or Lambda (6 MB limit).

resource "aws_s3_bucket" "uploads" {
  bucket        = "${local.name}-uploads-${local.suffix}"
  force_destroy = true # LAB ONLY so teardown works. Production: false (+ consider Object Lock).
}

resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket                  = aws_s3_bucket.uploads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  versioning_configuration {
    status = "Enabled" # an overwrite or delete never destroys a submitted file
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true # cuts KMS request costs by ~99%
  }
}

resource "aws_s3_bucket_cors_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  cors_rule {
    allowed_methods = ["POST"]
    allowed_origins = [local.site_url]
    allowed_headers = ["*"]
    max_age_seconds = 3000
  }
}

# 5-year retention: hot for marking, then progressively cheaper storage classes.
resource "aws_s3_bucket_lifecycle_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  rule {
    id     = "submissions-5-year-retention"
    status = "Enabled"

    filter {
      prefix = "submissions/"
    }

    transition {
      days          = 30 # marking finished -> infrequent access
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 180 # after the semester; millisecond retrieval for appeals
      storage_class = "GLACIER_IR"
    }

    transition {
      days          = 365 # compliance archive, ~12 h retrieval
      storage_class = "DEEP_ARCHIVE"
    }

    expiration {
      days = 1830 # 5 years (+ leap days buffer), then deleted
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.uploads]
}

data "aws_iam_policy_document" "uploads" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.uploads.arn, "${aws_s3_bucket.uploads.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "uploads" {
  bucket     = aws_s3_bucket.uploads.id
  policy     = data.aws_iam_policy_document.uploads.json
  depends_on = [aws_s3_bucket_public_access_block.uploads]
}

# New object -> SQS submissions queue -> submission-processor Lambda
resource "aws_s3_bucket_notification" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  queue {
    queue_arn     = aws_sqs_queue.main["submissions"].arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "submissions/"
  }

  depends_on = [aws_sqs_queue_policy.submissions]
}
