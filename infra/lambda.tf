resource "aws_lambda_function" "ingest" {
  function_name    = "${var.project}-ingest"
  role             = aws_iam_role.ingest.arn
  handler          = "handler.main"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 60
  memory_size      = 512
  filename         = "${path.module}/../build/ingest.zip"
  source_code_hash = filebase64sha256("${path.module}/../build/ingest.zip")

  environment {
    variables = {
      RAW_BUCKET       = aws_s3_bucket.raw.id
      PROCESSED_BUCKET = aws_s3_bucket.processed.id
    }
  }

  depends_on = [aws_cloudwatch_log_group.ingest]
}