terraform {
  required_version = ">= 1.11"

  backend "s3" {
    key          = "bedrock-rag-eval/terraform.tfstate"
    region       = "eu-central-1"
    encrypt      = true
    use_lockfile = true
  }
}