variable "project" {
  type    = string
  default = "bedrock-rag-eval"
}

variable "bucket_suffix" {
  description = "Unique suffix for globally-unique bucket names"
  type        = string
}