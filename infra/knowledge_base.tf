# Vector store and Knowledge Base for retrieval.
# The service role (aws_iam_role.kb) and its policy (aws_iam_role_policy.kb) live in iam.tf.

data "aws_bedrock_foundation_model" "titan_v2" {
  model_id = "amazon.titan-embed-text-v2:0"
}

data "aws_bedrock_foundation_model" "amazon_rerank" {
  model_id = local.models.rerank_model_id
}

resource "aws_s3vectors_vector_bucket" "vectors" {
  vector_bucket_name = "${var.project}-vectors"
}

resource "aws_s3vectors_index" "chunks" {
  index_name         = "chunks"
  vector_bucket_name = aws_s3vectors_vector_bucket.vectors.vector_bucket_name

  data_type       = "float32"
  dimension       = 1024
  distance_metric = "cosine"

  # The Knowledge Base stores chunk text in these keys. Left filterable, they
  # exceed the 2 KB filterable-metadata limit per vector and ingestion fails.
  metadata_configuration {
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
  }
}

resource "aws_bedrockagent_knowledge_base" "main" {
  name     = "${var.project}-kb"
  role_arn = aws_iam_role.kb.arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = data.aws_bedrock_foundation_model.titan_v2.model_arn
      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = aws_s3vectors_index.chunks.dimension
          embedding_data_type = "FLOAT32"
        }
      }
    }
  }

  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.chunks.index_arn
    }
  }

  # role_arn orders creation after the role, not after its policy.
  depends_on = [aws_iam_role_policy.kb]
}

resource "aws_bedrockagent_data_source" "chunks" {
  name                 = "processed-chunks"
  knowledge_base_id    = aws_bedrockagent_knowledge_base.main.id
  data_deletion_policy = "DELETE"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn         = aws_s3_bucket.processed.arn
      inclusion_prefixes = ["chunks/"]
    }
  }

  # The ingest Lambda does the chunking; each S3 object is one chunk.
  vector_ingestion_configuration {
    chunking_configuration {
      chunking_strategy = "NONE"
    }
  }
}

output "knowledge_base_id" {
  value = aws_bedrockagent_knowledge_base.main.id
}

output "data_source_id" {
  value = aws_bedrockagent_data_source.chunks.data_source_id
}
