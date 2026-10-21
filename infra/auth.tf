# Cognito issues the JWTs that the API's authorizer checks. Only the account owner creates users.

locals {
  # The CloudFront site only; feeds the callback and logout URLs here and CORS in api.tf.
  frontend_origins = [
    "https://${aws_cloudfront_distribution.frontend.domain_name}",
  ]
  login_domain = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${data.aws_region.current.region}.amazoncognito.com"
}

resource "aws_cognito_user_pool" "main" {
  name                = var.project
  user_pool_tier      = "ESSENTIALS"
  username_attributes = ["email"]
  mfa_configuration   = "OFF"
  deletion_protection = "ACTIVE"

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_domain" "main" {
  domain                = "${var.project}-${var.bucket_suffix}"
  user_pool_id          = aws_cognito_user_pool.main.id
  managed_login_version = 2
}

# Browser app: authorization code with PKCE. A public client has no secret to leak.
resource "aws_cognito_user_pool_client" "web" {
  name                                 = "${var.project}-web"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = [for origin in local.frontend_origins : "${origin}/"]
  logout_urls                          = [for origin in local.frontend_origins : "${origin}/"]
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  access_token_validity                = 60
  id_token_validity                    = 60
  refresh_token_validity               = 1

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

# Testing without a browser: AdminInitiateAuth only works with the owner's AWS credentials.
resource "aws_cognito_user_pool_client" "cli" {
  name                          = "${var.project}-cli"
  user_pool_id                  = aws_cognito_user_pool.main.id
  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  access_token_validity         = 60
  id_token_validity             = 60
  refresh_token_validity        = 1

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

resource "aws_cognito_managed_login_branding" "web" {
  client_id                   = aws_cognito_user_pool_client.web.id
  user_pool_id                = aws_cognito_user_pool.main.id
  use_cognito_provided_values = true
}

output "user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "web_client_id" {
  value = aws_cognito_user_pool_client.web.id
}

output "cli_client_id" {
  value = aws_cognito_user_pool_client.cli.id
}

output "login_domain" {
  value = local.login_domain
}
