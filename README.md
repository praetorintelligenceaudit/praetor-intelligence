# praetor-intelligence

Base repository for Praetor Intelligence.

## Google Cloud migration

This repository now includes a Google Cloud migration starter kit:

- [`docs/google-cloud-migration.md`](docs/google-cloud-migration.md): phased migration plan and operational checklist.
- [`infra/google-cloud`](infra/google-cloud): Terraform for Cloud Run, Artifact Registry, Secret Manager, IAM, and required APIs.
- [`cloudbuild.yaml`](cloudbuild.yaml): Cloud Build pipeline template for container build, push, and Cloud Run deployment.

The current repository does not yet include application source code or a `Dockerfile`. Add those before running the Cloud Build pipeline against a production project.
