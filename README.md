# PRAETOR Intelligence

Repositorio preparado para alojar el código fuente de **PRAETOR Intelligence**.

## Estado actual del repositorio

En este momento el repositorio contiene documentación inicial, pero **no incluye todavía el código fuente de la aplicación**. Por esa razón no es posible verificar de forma técnica la implementación real del backend, frontend, escáner, generación de PDF, Stripe, reportes o infraestructura.

Consulta [`TECHNICAL_AUDIT.md`](TECHNICAL_AUDIT.md) para ver la auditoría preliminar, el alcance de revisión y la lista exacta de evidencias necesarias para completar una auditoría técnica seria.

## Qué se necesita para completar la auditoría

Para poder auditar la app de punta a punta se debe incorporar al repositorio:

- Código del backend/API.
- Código del frontend.
- Configuración de despliegue.
- Dependencias y lockfiles.
- Variables de entorno de ejemplo sin secretos reales.
- Dockerfile, configuración Render u otra infraestructura usada.
- Tests, scripts de lint y documentación operativa.

## Próximo paso recomendado

Subir el código fuente completo y volver a ejecutar la auditoría técnica con pruebas reproducibles.
