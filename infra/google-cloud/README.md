# Google Cloud infrastructure

Terraform configuration for deploying Praetor Intelligence on Google Cloud with Cloud Run, Artifact Registry, Secret Manager, and a dedicated runtime service account.

## Prerequisites

- Google Cloud project with billing enabled.
- `gcloud` authenticated against the target project.
- Terraform 1.6 or newer.
- A container image for the application. The default image is a placeholder so the infrastructure can be validated before the application code is added.

## Usage

```bash
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan
terraform apply
```

To deploy a real application image, update `container_image` in `terraform.tfvars` to the Artifact Registry image produced by Cloud Build.

## Security notes

- Keep `allow_unauthenticated` set to `false` unless the service must be public.
- Add secret values with `gcloud secrets versions add`; Terraform only creates the secret containers.
- Store Terraform state in a protected remote backend before collaborating with multiple operators.
