terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

# Region and model IDs, shared with the query code (src/query/config.py reads the same file).
locals {
  models = jsondecode(file("${path.module}/../src/query/models.json"))
}

provider "aws" {
  region = local.models.region

  default_tags {
    tags = {
      project = "bedrock-rag-eval"
      env     = "dev"
    }
  }
}