# Static frontend: a private S3 bucket that only this CloudFront distribution can read.

resource "aws_s3_bucket" "frontend" {
  bucket = "${var.project}-frontend-${var.bucket_suffix}"
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${var.project}-frontend"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Three small files: serving them uncached means a deploy never needs a cache invalidation.
data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

# HSTS, nosniff, frame and referrer headers.
data "aws_cloudfront_response_headers_policy" "security" {
  name = "Managed-SecurityHeadersPolicy"
}

locals {
  frontend_origin_id = "frontend-s3"
  frontend_dir       = "${path.module}/../frontend"
  frontend_files = {
    "index.html" = "text/html; charset=utf-8"
    "app.js"     = "text/javascript; charset=utf-8"
  }
  # Read by app.js at startup, so no IDs or URLs are hardcoded in the page.
  frontend_config = {
    apiUrl      = aws_apigatewayv2_stage.default.invoke_url
    loginDomain = local.login_domain
    clientId    = aws_cognito_user_pool_client.web.id
  }
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  comment             = "${var.project} frontend"
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  http_version        = "http2and3"

  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = local.frontend_origin_id
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  default_cache_behavior {
    target_origin_id           = local.frontend_origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

# [H] The one IAM policy in this file: only this distribution may read objects, nothing may write.
data "aws_iam_policy_document" "frontend_bucket" {
  statement {
    sid       = "AllowCloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.frontend.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket     = aws_s3_bucket.frontend.id
  policy     = data.aws_iam_policy_document.frontend_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.frontend]
}

resource "aws_s3_object" "frontend" {
  for_each     = local.frontend_files
  bucket       = aws_s3_bucket.frontend.id
  key          = each.key
  source       = "${local.frontend_dir}/${each.key}"
  etag         = filemd5("${local.frontend_dir}/${each.key}")
  content_type = each.value
}

resource "aws_s3_object" "frontend_config" {
  bucket       = aws_s3_bucket.frontend.id
  key          = "config.json"
  content      = jsonencode(local.frontend_config)
  content_type = "application/json"
}

output "frontend_url" {
  value = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

# For local development: written to frontend/config.json, which is gitignored.
output "frontend_config" {
  value = local.frontend_config
}
