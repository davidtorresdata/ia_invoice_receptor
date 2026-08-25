# Capa Aplicación — Detalle clase por clase

Complemento del §3 de `desglose_componentes.md` (`app/application/`). Orquesta casos de uso sobre el dominio: no conoce FastAPI ni SQLAlchemy; solo puertos. Misma lógica de documentación que `capa_dominio_detallada.md`.

---

## Índice

1. [Servicios (puertos) — `services/`](#1-servicios-puertos--services)
2. [Casos de uso — `use_cases/`](#2-casos-de-uso--use_cases)
3. [DTOs — `dto/`](#3-dtos--dto)

---

## 1. Servicios (puertos) — `services/`

### `unit_of_work.py`

#### `class UnitOfWork(ABC)`
**Modela** el límite transaccional que agrega los cuatro repositorios del dominio, con las siguientes propiedades:

| Propiedad | Tipo | Función |
|---|---|---|
| `documents` | `DocumentRepository` | Persistencia de documentos dentro de la misma transacción. |
| `suppliers` | `SupplierRepository` | Proveedores con deduplicación por NIT. |
| `invoices` | `InvoiceRepository` | Facturas + renglones + read models. |
| `jobs` | `JobRepository` | Jobs de procesamiento asíncrono. |

Métodos:
- `commit()` *(abstract)* — Confirma atómicamente todos los cambios acumulados.
- `rollback()` *(abstract)* — Descarta cambios ante error.
- `__enter__()` / `__exit__()` *(abstract)* — Semántica `with`: el adaptador abre/cierra su sesión y hace rollback automático si hay excepción.

**Por qué existe**: un caso de uso toca varios agregados a la vez (documento + job + factura); sin UoW habría escrituras parciales. La tecnología concreta (sesión SQLAlchemy) es asunto del adaptador.

### `task_dispatcher.py`

#### `class TaskDispatcher(ABC)` *(puerto driven)*
Desacopla los casos de uso del broker. Un único método:
- `dispatch_invoice_processing(job_id)` — Encola el procesamiento del job dado; lanza `ExternalServiceError` si el broker no responde.

---

## 2. Casos de uso — `use_cases/`

### `upload_invoice.py` — Entrada al sistema

#### `@dataclass(frozen=True) class UploadCommand`
Entrada transport-agnostic del upload: `filename`, `content` (bytes) y `declared_mime` opcional. Tanto HTTP como tests construyen este objeto.

#### `@dataclass(frozen=True) class UploadResult`
Salida inmediata para el llamador: `document_id`, `job_id`, `filename`, `status`.

#### `class UploadInvoiceUseCase`
**Orquesta**: validar → almacenar → persistir (documento + job PENDING) → encolar. El caller recibe identificadores al instante; el trabajo pesado corre en workers.
- `__init__(uow_factory, storage, dispatcher, *, max_file_size_bytes)` — Recibe todo por inyección: fábrica de UoW, puerto de storage, puerto de dispatch y límite de tamaño.
- `execute(command) -> UploadResult` — Flujo completo:
  1. `_validate()`: tamaño vs cap → `FileTooLargeError`; luego identidad real del archivo vía `FileType.validate` (extensión+MIME+magic bytes).
  2. Crea `Document` (nombre saneado con `FileType.sanitize_filename`) y guarda el blob (`_store`), rellenando `storage_path`.
  3. Crea `ProcessingJob(document_id=...)` en PENDING.
  4. Abre UoW, persiste documento + job y confirma.
  5. **Solo después** de la persistencia duradera despacha a Celery (nunca jobs fantasma si la BD falla).
  - Loguea con `document_id/job_id/file_size/file_type` estructurados.
- `_validate(command) -> FileType` *(privado)* — Las dos primeras verificaciones del contrato de upload.
- `_store(document_id, safe_name, content) -> str` *(privado)* — Delegación directa al puerto `DocumentStorage`.

### `process_invoice.py` — El pipeline completo (ejecutado por workers)

#### `class ProcessInvoiceUseCase`
**Orquesta** las 6 etapas del procesamiento: guardias/transiciones → extracción de texto → extracción LLM/reglas → mapeo DTO→agregado → validación de negocio → persistencia.
- `__init__(uow_factory, storage, ocr_provider, extractor, validator, page_renderer=None)` — Todos los colaboradores son puertos; `page_renderer` es opcional (callable bytes+tipo → lista de PNG) para adaptadores de visión.
- `execute(job_id)` — Hilo conductor:
  1. `_begin()`: carga job/documento con guardias; `None` = ya completado o reanudado.
  2. Lee el blob del storage.
  3. OCR/texto vía `OCRProvider.extract_text`.
  4. `_safe_render()`: rasteriza páginas (fallo no fatal → sigue solo-texto).
  5. `_run_llm()`: extracción validada con métrica de duración.
  6. `_persist()`: dedup de proveedor + factura + estados finales.
  - Manejo de errores: `AppError` → `_handle_failure` y re-lanza; excepción inesperada se envuelve en `DocumentProcessingError` (los bugs nunca entran en loop de reintentos).
- `_begin(ctx)` *(privado)* — Guardias y transiciones:
  - Job inexistente → `JobNotFoundError`; documento faltante → `DocumentNotFoundError`.
  - Job ya `COMPLETED` → skip silencioso (idempotencia ante redelivery).
  - **Reanudación**: si una intento previo persistió la factura pero murió antes de cerrar el job, completa el job con esa factura en vez de duplicar trabajo.
  - Si procede: `job.start()` + `document.mark_processing()` + commit.
- `_safe_render(content, document_type, ctx)` *(privado)* — Nunca mata el pipeline: cualquier excepción de render degrada a `[]` con warning.
- `_run_llm(text, ctx, images)` *(privado)* — Llama al extractor (reglas→escalada según configuración) midiendo `elapsed_s`.
- `_persist(document, data, ctx)` *(privado)* — Todo en UNA transacción:
  1. Deduplica proveedor por `tax_id` (crea `Supplier` nuevo si no existe).
  2. `_build_invoice()` construye el agregado (items primero: la entidad exige ≥1).
  3. Valida con `InvoiceBusinessValidator` y adjunta el reporte.
  4. Guardia anti-duplicado concurrente: si otro worker ya insertó factura para este documento, completa el job con esa y termina.
  5. Inserta factura, marca documento `PROCESSED` y job `COMPLETED`; commit.
- `_build_invoice(document_id, supplier_id, data)` *(privado)* — Mapea `ExtractedInvoiceData` → agregado `Invoice`, envolviendo montos en `Money.parse` y guardando `raw_extraction=data.model_dump(mode="json")` para auditoría.
- `_handle_failure(job_id, exc, ctx)` *(privado)* — Estrategia central:
  - `exc.retryable=True` → deja estado PROCESSING (Celery re-entrará); log WARNING.
  - Permanente → cierra job como FAILED (mensaje truncado) y marca documento FAILED, en transacción protegida (si hasta esto falla, solo loguea).

### Consultas — `get_invoice.py`, `list_invoices.py`, `get_job_status.py`, `dashboard_stats.py`

Cuatro casos de uso mínimos, todos con el mismo patrón `(uow_factory)` + `execute()`:

#### `class GetInvoiceUseCase`
- `execute(invoice_id) -> tuple[Invoice, Supplier \| None]` — Trae la factura con ítems y su proveedor; `InvoiceNotFoundError` si no existe. Alimenta la vista de detalle.

#### `class ListInvoicesUseCase`
- `execute(criteria: InvoiceQuery) -> InvoiceListPage` — Listado filtrado/paginado (búsqueda, fechas, limit/offset). Delega directo al read model del repositorio.

#### `class GetJobStatusUseCase`
- `execute(job_id) -> ProcessingJob` — Estado actual del job (lo que poll-ea la UI cada 2s); `JobNotFoundError` si no existe.

#### `class DashboardStatsUseCase`
- `execute() -> DashboardStats` — Combina `jobs.count_by_status()` + `invoices.stats()` y descompone los contadores en pendientes/procesando/completados/fallidos más total de facturas y suma facturada.

---

## 3. DTOs — `dto/`

#### `@dataclass(frozen=True) class DashboardStats` (`dto/dashboard_stats.py`)
ViewModel de aplicación para el dashboard, con las propiedades `pending_jobs`, `processing_jobs`, `completed_jobs`, `failed_jobs` (ints), `total_invoices` (int) y `total_invoiced` (`Decimal`). Desacopla al presentador de las estructuras internas de repositorios.
