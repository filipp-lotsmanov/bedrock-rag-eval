# Every .pdf uploaded to the raw bucket invokes the ingest Lambda.

resource "aws_lambda_permission" "raw_upload" {
  statement_id   = "AllowRawBucketInvoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.ingest.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.raw.arn
  source_account = data.aws_caller_identity.current.account_id
}

# Manages the raw bucket's entire notification configuration.
resource "aws_s3_bucket_notification" "raw" {
  bucket = aws_s3_bucket.raw.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.ingest.arn
    events              = ["s3:ObjectCreated:*"]
    filter_suffix       = ".pdf"
  }

  # S3 checks it may invoke the function when the notification is created.
  depends_on = [aws_lambda_permission.raw_upload]
}
