# Capa Presentación — Detalle clase por clase

Complemento del §5 de `desglose_componentes.md` (`app/presentation/`). Dos frontales: la API REST (FastAPI) y la UI (Streamlit). Ninguno contiene lógica de negocio: traducen transporte ↔ casos de uso. Misma lógica que `capa_dominio_detallada.md`.

---

## Índice

1. [API — `presentation/api/`](#1-api--presentationapi)
2. [UI — `presentation/streamlit/`](#2-ui--presentationstreamlit)

---

## 1. API — `presentation/api/`

### `main.py`

#### `create_app() -> FastAPI`
Fábrica de la aplicación; orden de middleware **de afuera hacia adentro**:
1. `SecurityHeadersMiddleware` — headers duros en toda respuesta.
2. `RateLimitMiddleware(requests_per_minute=60, burst=10)`.
3. `CORSMiddleware` — orígenes desde `CORS_ORIGINS` (`"*"` solo por defecto de desarrollo; si no, lista separada por comas), métodos estándar.
Después registra los exception handlers, monta el router v1 bajo `/api/v1` (invoices, jobs, dashboard) y `/health` suelto. El módulo expone `app = create_app()` para uvicorn.

### `deps.py` — Proveedores FastAPI
Cinco funciones delgadas sobre la raíz de composición, una por caso de uso (`get_upload_invoice_use_case`, `get_get_invoice_use_case`, `get_list_invoices_use_case`, `get_job_status_use_case`, `get_dashboard_stats_use_case`). Son **la única costura** que los tests sobreescriben con `app.dependency_overrides` para correr la API contra fakes.

### `schemas.py` — Contratos de transporte (Pydantic v2)

| Schema | Modela |
|---|---|
| `UploadResponse` | Aceptado (202): document_id, job_id, filename, status, poll_url. |
| `JobResponse` | Estado completo del job: ids, status, attempts, error_message, celery_task_id, marcas temporales. |
| `SupplierResponse` | Datos fiscales del emisor para la vista. |
| `InvoiceItemResponse` | Renglón: description, quantity (Decimal), unit_price/tax/total (float para JSON). |
| `InvoiceResponse` | Factura completa + validation_report + raw_extraction (auditoría) + supplier + items. |
| `InvoiceSummaryResponse` | Fila de listado: totales + supplier_name/tax_id sin cargar agregados. |
| `PaginatedInvoicesResponse` | items + total + limit + offset. |
| `DashboardStatsResponse` | jobs {pending/processing/completed/failed}, invoices {total}, total_invoiced. |
| `ErrorBody` / `ErrorResponse` | Sobre de error uniforme `{error: {code, message}}`. |

### `mappers.py` — Entidad → schema
- `supplier_to_response(supplier)` — Proveedor plano.
- `invoice_to_response(invoice, supplier)` — Factura completa; montos Decimal→float; sin proveedor cargable usa placeholder "(unknown)".
- `summary_to_response(summary)` — Read model → fila de listado.
- `job_to_response(job)` — Job con status como string.
- `document_brief(document)` — Diccionario mínimo id/filename.

### `exception_handlers.py`
Traduce errores de dominio a HTTP consistente.
- `_STATUS_MAP` — Tabla tipo-excepción→código: NotFound*→404 · FileTooLarge→413 · EmptyFile/InvalidFile→400 · BusinessValidationError→422 · ExternalServiceError→502 · Persistence/DocumentProcessing→500.
- `_error_response(exc, http_status)` — Cuerpo `{error:{code,message}}`.
- `_request_ctx(request)` — método+ruta para logs.
- `register_exception_handlers(app)` — Instala tres handlers:
  - `handle_app_error` — Busca el match más específico en `_STATUS_MAP`; loguea WARNING (<500) o ERROR (≥500) con traceback; respuesta uniforme. Sin match → 500.
  - `handle_request_validation` — 422 con el primer error de validación legible (+conteo del resto).
  - `handle_unexpected` — Cualquier excepción no mapeada: log CRITICAL con detalle pero **respuesta sanitizada** `internal_error` (nada de stack traces al cliente).

### `routers/invoices.py` — prefix `/invoices`
- `upload_invoice(file, use_case) POST /upload → 202` — Lee el archivo con cap streaming (`_read_limited`: chunks de 1 MB; excede límite → `FileTooLargeError`; vacío → `InvalidFileError`), construye `UploadCommand` y ejecuta el use case; responde con identificadores + poll_url inmediatos.
- `list_invoices(...) GET ""` — Query params validados (search ≤120, limit 1..100, offset ≥0, fechas); arma `InvoiceQuery` vía container y devuelve página de resúmenes.
- `get_invoice(invoice_id) GET /{id}` — Detalle completo factura+proveedor.
- `_read_limited(file)` *(helper)* — Descarga acotada: nunca bufferiza más que el máximo permitido.

### `routers/jobs.py` — prefix `/jobs`
- `get_job(job_id) GET /{job_id}` — Estado del job (lo que poll-ea la UI cada 2s hasta estado terminal).

### `routers/dashboard.py` — prefix `/dashboard`
- `get_stats() GET /stats` — Ejecuta `DashboardStatsUseCase` y remodela contadores para la UI.

### `routers/health.py`
- `health(response) GET /health` — Probe vivo/listo: chequea componentes, 200 "ok" o 503 "degraded" según `all(components)`.
- `_check_database()` — `SELECT 1` sobre el engine del container; excepción → "down" (logueada).
- `_check_redis()` — Cliente redis efímero (timeouts 2s) + PING.

---

## 2. UI — `presentation/streamlit/`

Regla arquitectónica: la UI **nunca** toca PostgreSQL ni objetos de dominio; solo intercambia JSON con la API.

### `app.py` — Shell multipágina
Configura logging idéntico al api/worker (formatter trazable + excepthooks), define las 4 páginas (`dashboard`, `upload_invoice`, `invoices`, `invoice_detail`) con `st.navigation`, y pinta la barra lateral con el título y la URL de la API. Cero lógica de negocio.

### `api_client.py` — El único canal hacia el backend

#### `class ApiError(Exception)`
Error amigable para la UI con `status_code` y `code` del sobre de error de la API.

#### `get_api_client()` *(decorado con `@st.cache_resource`)*
Singleton por sesión Streamlit; base URL desde env `API_BASE_URL`.

#### `class ApiClient`
- `__init__(base_url, timeout=15.0)` — Cliente httpx persistente.
- Llamadas: `upload(filename, content, content_type)` (multipart, timeout 60s) · `get_invoice(id)` · `list_invoices(**params)` (limpia params vacíos/"ALL") · `get_job(job_id)` · `stats()`.
- `_request(method, path, **kwargs)` — Traduce fallos httpx → `ApiError("Cannot reach the API...")`; status ≥400 extrae `{error:{code,message}}` del sobre uniforme.

### `pages/dashboard.py`
Métricas de procesamiento desde `/api/v1/dashboard/stats`: total facturas, suma facturada, y los 4 contadores de jobs en columnas; botón Refresh; errores de API detienen la página con mensaje claro.

### `pages/upload_invoice.py`
- Uploader restringido a pdf/png/jpg/jpeg con info de tamaño/MIME.
- Botón Process: `client.upload(...)` → guarda `last_job_id` en sesión → `poll_job()`.
- `poll_job(job_id)` — Polling cada 2s con tope duro de 5 minutos: COMPLETED → éxito + botón "Open invoice detail" (switch_page); FAILED → descompone el mensaje por `;` en bullets legibles; intermedio → barra de progreso proporcional con estado e intento actual.

### `pages/invoices.py`
Búsqueda por texto (número/proveedor) + rango de fechas → tabla pandas con selección de fila simple; seleccionar + "Open detail" fija `selected_invoice_id` y navega al detalle.

### `pages/invoice_detail.py`
Vista 360° de una factura:
- Cabecera número—proveedor y 4 métricas (total/subtotal/tax/ítems).
- Bloques Supplier (tabla markdown con NIT/dirección/contacto) y Document (ids JSON).
- Renglones como dataframe con Σ ítems vs subtotal declarado (chequeo visual).
- Expander Validation report: issues ordenados por severidad con iconos ❌/⚠️/ℹ️, código y campo culpable.
- Expander Raw extraction: payload crudo del extractor en JSON descargable (`{numero}.extraction.json`) — auditoría.
- Botón volver.
