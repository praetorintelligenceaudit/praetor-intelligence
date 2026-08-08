variable "project_id" {
  description = "Google Cloud project ID where the infrastructure will be created."
  type        = string
}

variable "region" {
  description = "Google Cloud region used for regional resources."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
  default     = "praetor-intelligence"
}

variable "artifact_repository_id" {
  description = "Artifact Registry Docker repository ID."
  type        = string
  default     = "praetor-intelligence"
}

variable "container_image" {
  description = "Container image deployed to Cloud Run. Replace after the first Cloud Build run."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "allow_unauthenticated" {
  description = "Whether Cloud Run should allow public unauthenticated access."
  type        = bool
  default     = false
}

variable "environment_variables" {
  description = "Non-sensitive environment variables for the Cloud Run service."
  type        = map(string)
  default     = {}
}

variable "secret_names" {
  description = "Secret Manager secret IDs to create for the application. Secret values must be added out-of-band."
  type        = set(string)
  default     = []
}
