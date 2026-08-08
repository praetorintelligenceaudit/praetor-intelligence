output "artifact_registry_repository" {
  description = "Artifact Registry repository resource name."
  value       = google_artifact_registry_repository.docker.name
}

output "cloud_run_service_uri" {
  description = "Cloud Run service URI."
  value       = google_cloud_run_v2_service.application.uri
}

output "runtime_service_account_email" {
  description = "Cloud Run runtime service account email."
  value       = google_service_account.cloud_run.email
}
