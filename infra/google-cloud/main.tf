provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_project_service" "required" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
  ])

  project = var.project_id
  service = each.value

  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "docker" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_repository_id
  description   = "Docker images for ${var.service_name}."
  format        = "DOCKER"

  depends_on = [google_project_service.required]
}

resource "google_service_account" "cloud_run" {
  project      = var.project_id
  account_id   = "${var.service_name}-run"
  display_name = "${var.service_name} Cloud Run runtime"

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "application" {
  for_each = var.secret_names

  project   = var.project_id
  secret_id = each.value

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "runtime_access" {
  for_each = google_secret_manager_secret.application

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cloud_run.email}"
}

resource "google_cloud_run_v2_service" "application" {
  project  = var.project_id
  name     = var.service_name
  location = var.region

  template {
    service_account = google_service_account.cloud_run.email

    containers {
      image = var.container_image

      dynamic "env" {
        for_each = var.environment_variables

        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  depends_on = [
    google_artifact_registry_repository.docker,
    google_project_service.required,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  count = var.allow_unauthenticated ? 1 : 0

  project  = var.project_id
  location = google_cloud_run_v2_service.application.location
  name     = google_cloud_run_v2_service.application.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
