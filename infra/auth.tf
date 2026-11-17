# Amazon Cognito: role-based sign-in. Groups = roles; the "cognito:groups" claim
# in the ID token is what the API checks. Production: federate with the
# university identity provider (SAML / OIDC, e.g. Microsoft Entra ID).

resource "aws_cognito_user_pool" "main" {
  name                     = local.name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = "INACTIVE" # production: ACTIVE

  admin_create_user_config {
    allow_admin_create_user_only = true # no self sign-up: administrators create accounts
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  mfa_configuration = "OPTIONAL" # TOTP; enforce for staff in production
  software_token_mfa_configuration {
    enabled = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_group" "roles" {
  for_each = {
    Admins   = { precedence = 10, description = "Manage users and exam schedules" }
    Tutors   = { precedence = 20, description = "Create exams and assignments, mark and release results" }
    Students = { precedence = 30, description = "Sit exams and upload assignments" }
  }

  user_pool_id = aws_cognito_user_pool.main.id
  name         = each.key
  precedence   = each.value.precedence
  description  = each.value.description
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${local.name}-${local.suffix}"
  user_pool_id = aws_cognito_user_pool.main.id
}

# Public SPA client: authorization code flow with PKCE, no client secret.
resource "aws_cognito_user_pool_client" "web" {
  name         = "${local.name}-web"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${local.site_url}/"]
  logout_urls                          = ["${local.site_url}/"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true

  # Short-lived ID token; the SPA silently refreshes it so a 3-hour exam never logs out.
  id_token_validity      = 60
  access_token_validity  = 60
  refresh_token_validity = 12
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "hours"
  }
}
