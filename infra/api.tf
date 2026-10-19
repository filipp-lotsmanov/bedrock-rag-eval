# Public HTTPS entry point. API Gateway validates each JWT before the Lambda runs.

resource "aws_apigatewayv2_api" "http" {
  name          = var.project
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = local.frontend_origins
    allow_methods = ["POST"]
    allow_headers = ["authorization", "content-type"]
    max_age       = 300
  }
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.http.id
  name             = "cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  # Checks signature, expiry, issuer, and aud (ID tokens) or client_id (access tokens).
  jwt_configuration {
    issuer   = "https://${aws_cognito_user_pool.main.endpoint}"
    audience = [aws_cognito_user_pool_client.web.id, aws_cognito_user_pool_client.cli.id]
  }
}

resource "aws_apigatewayv2_integration" "query" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.query.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "query" {
  api_id             = aws_apigatewayv2_api.http.id
  route_key          = "POST /query"
  target             = "integrations/${aws_apigatewayv2_integration.query.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  # Rejects floods before they reach Lambda; the reranker quota (2/min) is the hard cap behind it.
  default_route_settings {
    throttling_burst_limit = 5
    throttling_rate_limit  = 1
  }
}

resource "aws_lambda_permission" "api" {
  statement_id  = "AllowHttpApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.query.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/POST/query"
}

output "api_url" {
  value = aws_apigatewayv2_stage.default.invoke_url
}
