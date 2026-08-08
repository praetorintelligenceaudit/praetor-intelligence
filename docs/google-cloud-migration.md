# Plan de migración a Google Cloud

Este documento describe una ruta práctica para trasladar **Praetor Intelligence** a Google Cloud. El repositorio actual solo contiene documentación, así que la infraestructura queda preparada como base para desplegar una aplicación contenedorizada cuando se incorpore el código ejecutable.

## Arquitectura objetivo

La propuesta inicial prioriza servicios administrados para reducir operación:

- **Cloud Run** para ejecutar la aplicación en contenedores con escalado automático.
- **Artifact Registry** para almacenar imágenes Docker.
- **Cloud Build** para construir y publicar imágenes desde el repositorio.
- **Secret Manager** para variables sensibles y credenciales.
- **Cloud Logging y Cloud Monitoring** para observabilidad.
- **IAM con cuentas de servicio dedicadas** para aplicar mínimo privilegio.

## Fases recomendadas

### 1. Preparación del proyecto GCP

1. Crear o seleccionar un proyecto de Google Cloud.
2. Vincular una cuenta de facturación.
3. Activar las APIs necesarias:
   - Cloud Run Admin API
   - Cloud Build API
   - Artifact Registry API
   - Secret Manager API
   - IAM API
4. Crear un bucket remoto para el estado de Terraform si se va a compartir la gestión de infraestructura.

### 2. Contenerización de la aplicación

Cuando exista código de aplicación en este repositorio:

1. Añadir un `Dockerfile` en la raíz o en el directorio del servicio.
2. Exponer el puerto leído desde la variable de entorno `PORT`.
3. Externalizar configuración mediante variables de entorno.
4. Mover secretos a Secret Manager, nunca al repositorio.

### 3. Aprovisionamiento de infraestructura

La carpeta `infra/google-cloud` contiene una base Terraform para crear:

- Repositorio Docker en Artifact Registry.
- Cuenta de servicio de ejecución para Cloud Run.
- Servicio Cloud Run parametrizable.
- Secretos base en Secret Manager.

### 4. CI/CD

El archivo `cloudbuild.yaml` define una plantilla de pipeline para:

1. Construir la imagen del contenedor.
2. Publicarla en Artifact Registry.
3. Desplegarla en Cloud Run.

Antes de usarlo, confirma que existe un `Dockerfile` y que las sustituciones de Cloud Build coinciden con tu proyecto.

## Comandos de referencia

```bash
cd infra/google-cloud
terraform init
terraform plan -var="project_id=TU_PROYECTO" -var="region=us-central1"
terraform apply -var="project_id=TU_PROYECTO" -var="region=us-central1"
```

Para ejecutar el pipeline manualmente:

```bash
gcloud builds submit \
  --config=cloudbuild.yaml \
  --substitutions=_REGION=us-central1,_SERVICE_NAME=praetor-intelligence,_REPOSITORY=praetor-intelligence
```

## Pendientes antes del primer despliegue real

- Incorporar el código de la aplicación.
- Añadir el `Dockerfile` correspondiente.
- Definir variables de entorno no sensibles.
- Crear secretos reales en Secret Manager.
- Configurar dominio, certificados y políticas de acceso si el servicio será público.
- Añadir pruebas automatizadas al pipeline.
