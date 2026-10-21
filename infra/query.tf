# Query Lambda: retrieve with reranking, then generate with Nova Lite through the EU profile.

data "aws_bedrock_inference_profile" "nova_lite" {
  inference_profile_id = local.models.generation_model_id
}

resource "aws_cloudwatch_log_group" "query" {
  name              = "/aws/lambda/${var.project}-query"
  retention_in_days = 14
}

resource "aws_iam_role" "query" {
  name               = "${var.project}-query"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "query_permissions" {
  statement {
    sid     = "Logs"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      aws_cloudwatch_log_group.query.arn,
      "${aws_cloudwatch_log_group.query.arn}:*",
    ]
  }

  # Reranking runs inside Retrieve under the Knowledge Base's own role, so Retrieve is enough here.
  statement {
    sid       = "Retrieve"
    effect    = "Allow"
    actions   = ["bedrock:Retrieve"]
    resources = [aws_bedrockagent_knowledge_base.main.arn]
  }

  # A geographic profile needs the profile itself and the model in every Region it routes to.
  statement {
    sid     = "Generate"
    effect  = "Allow"
    actions = ["bedrock:InvokeModel"]
    resources = concat(
      [data.aws_bedrock_inference_profile.nova_lite.inference_profile_arn],
      [for model in data.aws_bedrock_inference_profile.nova_lite.models : model.model_arn],
    )
  }
}

resource "aws_iam_role_policy" "query" {
  name   = "${var.project}-query-permissions"
  role   = aws_iam_role.query.id
  policy = data.aws_iam_policy_document.query_permissions.json
}

resource "aws_lambda_function" "query" {
  function_name = "${var.project}-query"
  role          = aws_iam_role.query.arn
  handler       = "query.handler.main"
  runtime       = "python${trimspace(file("${path.module}/lambda_python_version"))}"
  architectures = ["arm64"]
  # API Gateway stops waiting after 30 seconds.
  timeout          = 25
  memory_size      = 512
  filename         = "${path.module}/../build/query.zip"
  source_code_hash = filebase64sha256("${path.module}/../build/query.zip")

  environment {
    variables = {
      KNOWLEDGE_BASE_ID = aws_bedrockagent_knowledge_base.main.id
    }
  }

  depends_on = [aws_cloudwatch_log_group.query, aws_iam_role_policy.query]
}

output "query_function_name" {
  value = aws_lambda_function.query.function_name
}
