# Desglose Detallado de Componentes — Procesador de Facturas

> Documento de referencia que expone **cada archivo del proyecto**: su propósito,
> sus importaciones, sus clases/funciones con firmas y la razón por la cual fue
> creado. Complementa `docs/manual_tecnico.md` (decisiones) y
> `docs/arquitectura.drawio` (diagrama).

---

## Índice

1. [Visión general](#1-visión-general)
2. [Capa Dominio — `app/domain/`](#2-capa-dominio)
3. [Capa Aplicación — `app/application/`](#3-capa-aplicación)
4. [Capa Infraestructura — `app/infrastructure/`](#4-capa-infraestructura)
5. [Capa Presentación — `app/presentation/`](#5-capa-presentación)
6. [Configuración — `app/config/`](#6-configuración)
7. [Tests — `tests/`](#7-tests)
8. [Migraciones — `alembic/`](#8-migraciones)
9. [Raíz del repositorio y scripts](#9-raíz-del-repositorio-y-scripts)

---

## 1. Visión general

Sistema de procesamiento asíncrono de facturas con arquitectura **hexagonal**
(puertos y adaptadores). Flujo de alto nivel:

```
Streamlit / HTTP  →  FastAPI  →  Casos de Uso  →  PostgreSQL (vía UoW)
                     (cola)         │
                                    ▼
                          Celery worker ("invoices")
                                    │
                     OCR (texto embebido | PaddleOCR-VL | PP-OCR | Tesseract)
                                    │
                     Extracción híbrida: Reglas → escalada según LLM_EXECUTION
                         ├─ api   → modelo visión OpenAI-compatible (Gemini)
                         └─ local → fusión OCR + reglas (100% offline)
                                    │
                     Validación de negocio → persistencia factura/proveedor
```

**Stack**: Python 3.12 · FastAPI · SQLAlchemy 2 (psycopg3) · Celery + Redis ·
PostgreSQL 16 · Streamlit · PaddleOCR (VL y PP-OCR) · Tesseract · pypdfium2 ·
Alembic · pytest · ruff.

**Regla de oro de dependencias**: `dominio ← aplicación ← {infraestructura,
presentación}`. El dominio no importa nada de las capas externas.

---

## 2. Capa Dominio

El corazón del sistema: entidades, objetos de valor, excepciones y **puertos**
(interfaces). Cero dependencias de frameworks.

### 2.1 `app/domain/__init__.py`
- **Propósito**: marcar el paquete raíz del dominio.
- **Por qué**: convención de paquete; sin lógica para mantener el dominio limpio.

### 2.2 `app/domain/entities/invoice.py`
- **Importaciones**: `dataclasses`, `datetime`, `decimal.Decimal`, VOs del dominio (`Money`, enums).
- **Contenido**:
  - `@dataclass Supplier` — proveedor: `id`, `name`, `tax_id`, `address`, `phone`, `email`, timestamps.
  - `@dataclass InvoiceItem` — línea de factura: `id`, `description`, `quantity: Decimal`, `unit_price: Decimal`, `tax_amount: Decimal`, `total: Money`.
  - `@dataclass Invoice` (**agregado raíz**) — `document_id` (ancla de idempotencia 1:1), `supplier_id`, `number`, fechas, `currency`, trío monetario como `Money`, `validation_report`, `raw_extraction`, `items`.
    - `__post_init__`: valida invariantes (≥1 ítem; coherencia matemática `subtotal + tax ≈ total` dentro de tolerancia).
- **Por qué**: encapsula las reglas de negocio de una factura en un único agregado consistente. Nada externo puede crear una factura inválida.

### 2.3 `app/domain/entities/document.py`
- **Contenido**: entidad `Document` — `filename`, `content_type`, `size_bytes`, `storage_path`, `document_type: DocumentType`, `status: DocumentStatus`; métodos de transición (`mark_processed()`, `mark_failed()`), propiedad `is_processed`.
- **Por qué**: modelar el ciclo de vida del archivo subido independiente de dónde viva físicamente.

### 2.4 `app/domain/entities/job.py`
- **Contenido**: entidad `ProcessingJob` — estado (`JobStatus`), `attempts`, `invoice_id`, `celery_task_id`, `error_message`, `started_at/finished_at`; transiciones `start()`, `complete(invoice_id)`, `fail(message)`, `attach_task(task_id)`; propiedad `is_terminal`.
- **Por qué**: el seguimiento del trabajo asíncrono es un concepto de negocio (con estados válidos), no un detalle de Celery.

### 2.5 `app/domain/value_objects/money.py`
- **Importaciones**: `decimal` (`Decimal`, `ROUND_HALF_UP`, `InvalidOperation`), `dataclasses`.
- **Contenido**:
  - `class MoneyError(ValueError)`.
  - `@dataclass(frozen=True, slots=True, order=True) class Money` — campo `amount: Decimal`.
    - `__post_init__`: exige `Decimal` finito **no negativo** y cuantiza a 2 decimales (`ROUND_HALF_UP`) vía `object.__setattr__`.
    - `parse(raw, *, default="0.00") -> Money`: entrada defensiva de JSON/LLM/formularios.
    - `_normalize(text) -> str`: acepta formatos europeos/mixtos (`'1.234,56'` → `'1234.56'`, `'1,234.56'` → `'1234.56'`).
    - `add`, `__add__`, `__radd__` (soporta `sum()`), `multiply(factor)`, `is_close(other, tolerance=0.01)`, `__str__`.
- **Por qué**: el dominio nunca opera floats monetarios; garantiza redondeo determinista y comparaciones seguras (tolerancia de centavos).

### 2.6 `app/domain/value_objects/enums.py`
- **Importaciones**: `enum.StrEnum`.
- **Contenido**:
  - `JobStatus(StrEnum)`: `PENDING → PROCESSING → COMPLETED|FAILED`; propiedad `is_terminal`.
  - `DocumentStatus(StrEnum)`: `RECEIVED, PROCESSING, PROCESSED, FAILED`.
  - `DocumentType(StrEnum)`: `PDF | IMAGE` — clasificación que decide la estrategia de extracción.
- **Por qué**: máquinas de estado explícitas; `StrEnum` permite persistir el valor legible directamente en VARCHAR.

### 2.7 `app/domain/value_objects/file_type.py`
- **Importaciones**: `unicodedata`, `dataclasses`, excepciones del dominio.
- **Constantes**: `ALLOWED_EXTENSIONS = {pdf, png, jpg, jpeg}`; `DECLARED_MIME_TYPES`; `_MAGIC_SIGNATURES` (`%PDF-`, PNG `\x89PNG\r\n\x1a\n`, JPEG `\xff\xd8\xff`); `_PDF_SEARCH_WINDOW = 1024`.
- **Contenido**: `@dataclass(frozen=True) FileType(document_type, extension, signature_format, mime_type)`
  - `sanitize_filename(filename) -> str`: basename seguro (NFKD→ASCII, solo `[A-Za-z0-9._-]`).
  - `validate(*, content, filename, declared_mime) -> FileType`: 5 reglas (no vacío, extensión blanca, MIME blanco, magic bytes, coherencia familia↔extensión↔MIME).
  - `_sniff(content)` + helpers de MIME por defecto.
- **Por qué**: validación de identidad de archivo **sin libmagic ni dependencias**, 100% testeable; evita subir ejecutables disfrazados o PDFs corruptos.

### 2.8 `app/domain/value_objects/extracted_invoice.py`
- **Contenido**: esquema `ExtractedInvoice` (+ sub-esquemas proveedor/líneas) que normaliza lo que produce **cualquier** extractor (LLM, reglas, mock): campos requeridos, opcionales y conversión a `Money.parse`.
- **Por qué**: contrato único entre extracción y construcción del agregado; desacopla el formato crudo de cada proveedor.

### 2.9 `app/domain/value_objects/validation.py`
- **Contenido**: VO(s) para el reporte de validación de negocio (lista de issues estructurados, veredicto) que se persiste en `invoices.validation_report` (JSONB).
- **Por qué**: auditar *por qué* una factura pasó o falló las reglas, sin perder trazabilidad.

### 2.10 `app/domain/exceptions.py`
- **Jerarquía** (todas heredan de `AppError(message, *, retryable=None)` con `error_code`):
  - `DomainError` → `EntityValidationError`, `BusinessValidationError(message, issues)`.
  - `ApplicationError` → `NotFoundError` (`DocumentNotFoundError`, `JobNotFoundError`, `InvoiceNotFoundError`), `InvalidFileError` (`FileTooLargeError`, `EmptyFileError`), `DocumentProcessingError` → `TransientPipelineError` (retryable).
  - `ExternalServiceError` (retryable por naturaleza) → `OCRExtractionError`, `LLMExtractionError` → `PartialExtractionError(message, *, partial_data, missing_fields)`, `StorageError`.
  - `PersistenceError` (retryable), `ConfigurationError`.
- **Contrato clave**: `retryable=True/False` es consumido por la tarea Celery para decidir reintentos con backoff vs. fallo definitivo.
- **Por qué**: semántica de error unificada entre API, worker y tests; `PartialExtractionError` transporta los datos parciales que la extracción híbrida fusiona después.

### 2.11 Puertos de servicio — `app/domain/services/`
Interfaces (Protocol/ABC) que el dominio define y la infraestructura implementa:

| Archivo | Puerto | Métodos | Por qué |
|---|---|---|---|
| `ocr_provider.py` | `OCRProvider` | `extract_lines(content, document_type) -> list[str]` | abstraer el motor OCR real |
| `invoice_extractor.py` | `InvoiceExtractor` | `extract(pages_images, text_lines, document_type) -> ExtractedInvoice` | abstraer reglas/LLM/mock |
| `invoice_validator.py` | `InvoiceBusinessValidator` (implementación concreta en dominio) | `validate(extracted) -> ValidationReport` | las reglas de negocio son conocimiento del dominio |
| `document_storage.py` | `DocumentStorage` | `save(...)`, `open(...)`, ... | abstraer el filesystem/blob storage |
| `page_renderer.py` | (firma usada por casos de uso) | renderiza páginas a imágenes | abstraer PDF/TIFF → imágenes |

### 2.12 Puertos de repositorio — `app/domain/repositories/`
- `document_repository.py` — `DocumentRepository`: `add`, `get`, `update`.
- `job_repository.py` — `JobRepository`: `add`, `get`, `update`, `count_by_status`.
- `supplier_repository.py` — `SupplierRepository`: `add(supplier) -> Supplier`, `get`, `find_by_tax_id(tax_id)` (deduplicación por NIT).
- `invoice_repository.py` — `InvoiceRepository` + criterios:
  - `InvoiceQuery(search, date_from, date_to, limit=20, offset=0)`
  - `InvoiceSummary`, `InvoiceListPage(items, total_count)`, `InvoiceStats(total_invoices, total_invoiced)`
  - métodos: `add`, `get`, `get_by_document`, `query(criteria)`, `stats()`.
- **Por qué**: el dominio declara QUÉ necesita persistir; SQLAlchemy decide CÓMO. Permite los fakes en memoria de los tests.

---

## 3. Capa Aplicación

Orquestación de casos de uso. Depende solo del dominio (y de sus propios puertos).

### 3.1 `app/application/services/unit_of_work.py`
- **Contenido**: ABC/puerto `UnitOfWork` con atributos `documents`, `suppliers`, `invoices`, `jobs` y `commit()`/`rollback()`; uso como context manager.
- **Por qué**: garantizar **una transacción por operación de negocio** cruzando varios repositorios, sin acoplar al dominio a SQLAlchemy.

### 3.2 `app/application/services/task_dispatcher.py`
- **Contenido**: puerto `TaskDispatcher.dispatch_invoice_processing(job_id) -> None`.
- **Por qué**: la capa de aplicación "pide" encolar trabajo sin saber que existe Celery/Redis.

### 3.3 `app/application/use_cases/upload_invoice.py`
- **Constructor**: `UploadInvoiceUseCase(uow_factory, storage: DocumentStorage, dispatcher: TaskDispatcher, max_file_size_bytes)`.
- **`execute(filename, content, declared_mime) -> resultado de upload`**:
  1. `FileType.validate(...)` (extensión+MIME+magic bytes) y límite de tamaño.
  2. Persiste `Document(RECEIVED)` + `ProcessingJob(PENDING)` en **una** transacción.
  3. Guarda bytes vía `storage.save(...)` y despacha `process_invoice`.
- **Por qué**: única puerta de entrada de archivos; valida antes de gastar recursos y deja todo atómico.

### 3.4 `app/application/use_cases/process_invoice.py`
- **Constructor**: `ProcessInvoiceUseCase(uow_factory, storage, ocr_provider: OCRProvider, extractor: InvoiceExtractor, validator, page_renderer)`.
- **`execute(job_id) -> None`** — pipeline completo del worker:
  1. Carga job+documento; marca `PROCESSING`/inicia tiempos.
  2. `storage.open(path)` → contenido binario.
  3. `ocr_provider.extract_lines(...)` → líneas de texto.
  4. `page_renderer(contenido, tipo)` → imágenes de página (para visión).
  5. `extractor.extract(images, lines, doc_type)` → `ExtractedInvoice`.
  6. Construye agregado `Invoice` + dedup de proveedor por `tax_id`.
  7. `validator.validate(...)` → reporte de negocio.
  8. Persiste invoice+supplier+job `COMPLETED` en una transacción.
  9. Ante `AppError`: persiste `FAILED` (si corresponde) y re-lanza para que la tarea decida reintentos.
- **Por qué**: es EL caso de negocio; concentra la secuencia OCR→extracción→validación→persistencia con semántica transaccional clara.

### 3.5 Consultas — `get_invoice.py`, `list_invoices.py`, `get_job_status.py`, `dashboard_stats.py`
- `GetInvoiceUseCase(uow_factory).execute(invoice_id) -> detalle` (factura + proveedor + ítems; `InvoiceNotFoundError` si falta).
- `ListInvoicesUseCase(uow_factory).execute(query: InvoiceQuery) -> InvoiceListPage` (búsqueda, rango de fechas, paginación).
- `GetJobStatusUseCase(uow_factory).execute(job_id) -> estado del job` (para polling del front).
- `DashboardStatsUseCase(uow_factory).execute() -> DashboardStats`.
- **Por qué separarlos**: lecturas simples sin lógica; cada uno es testeable y reutilizable (API y dashboard comparten).

### 3.6 `app/application/dto/dashboard_stats.py`
- **Contenido**: dataclass `DashboardStats` (conteos por estado de job/factura, total facturado…).
- **Por qué**: DTO neutro que la presentación mapea a su esquema propio.

---

## 4. Capa Infraestructura

Implementaciones concretas de todos los puertos + composición + mensajería.

### 4.1 `app/infrastructure/container.py` — **raíz de composición (DI)**
- **Importaciones**: `functools.lru_cache`, SQLAlchemy, todos los casos de uso, `Settings/get_settings`, adaptadores (Celery dispatcher, UoW, storage, OCR, extractor factories).
- **Funciones**:
  - Settings/DB: `get_app_settings()`, `get_engine()` (@lru_cache), `get_session_factory()` (@lru_cache), `build_uow()` (UoW fresco por ejecución → seguro entre hilos).
  - Adaptadores singleton: `get_document_storage()`, `get_task_dispatcher()`, `_get_ocr_provider()`, `_get_invoice_extractor()`, `_get_invoice_validator()`.
  - Casos de uso (nuevos por llamada, stateless): `build_upload_invoice_use_case()`, `build_process_invoice_use_case()` (inyecta `render_page_images` con `vision_max_pages`), `build_get_invoice_use_case()`, `build_list_invoices_use_case()`, `build_job_status_use_case()`, `build_dashboard_stats_use_case()`, `build_invoice_query(**kwargs)`.
- **Por qué**: **único módulo que conoce clases concretas**. FastAPI (deps) y Celery (tasks) importan de aquí ⇒ cableado idéntico en API y worker; cambiar un adaptador toca un solo archivo.

### 4.2 Paquete `celery_app/`
#### `app.py`
- Crea `celery_app = Celery("invoice_processor", broker=redis_url, backend=redis_url, include=[...tasks])`.
- Config: cola única `"invoices"`, `task_acks_late=True` (reparto si muere el worker), `worker_prefetch_multiplier=1` (reparto justo), `task_track_started=True`, límites duro/blando desde settings, `result_expires=3600`, UTC, `worker_hijack_root_logger=False`.
- Import final de `signals` para registrar handlers.
- **Por qué**: centralizar broker/backend/defaults; acks_late+prefetch 1 dan fiabilidad ante caídas.

#### `dispatcher.py`
- `class CeleryTaskDispatcher(TaskDispatcher)` — `dispatch_invoice_processing(job_id)`: normaliza UUID, `send_task("process_invoice", args=[...], queue="invoices")`; traduce errores kombu (`OperationalError`, `TimeoutError`, `ConnectionError`) a `ExternalServiceError(retryable)`.
- **Por qué**: adaptador del puerto de aplicación; la app nunca toca Celery directamente.

#### `tasks.py`
- `@celery_app.task(bind=True, name="process_invoice", max_retries=settings.celery_max_retries)` sobre `process_invoice_task(self, job_id: str) -> dict`:
  1. Log contextual + `_attach_celery_task_id` (trazabilidad DB↔broker).
  2. Ejecuta `ProcessInvoiceUseCase`.
  3. `SoftTimeLimitExceeded` → `_mark_job_failed(timeout)`.
  4. `AppError` → `_handle_failure(...)`.
- `_handle_failure(task, exc, ctx)`: si `exc.retryable` y quedan intentos → `task.retry(countdown=backoff*2^n)` (backoff exponencial); si permanente → devuelve FAILED (el caso de uso ya persistió); si agotó reintentos → `_mark_job_failed` y termina.
- `_attach_celery_task_id`, `_mark_job_failed`: helpers con UoW propio, tolerantes a fallo (solo observabilidad).
- **Por qué**: política de reintentos explícita y separada del caso de uso; la BD queda como fuente de verdad aunque el broker pierda mensajes.

#### `signals.py`
- `@signals.setup_logging` → instala el formatter de la app en workers.
- `@signals.task_failure` → log ERROR con nombre/id de task, args y traceback completo.
- **Por qué**: trazabilidad total de fallos sin el logging por defecto de Celery.

### 4.3 Paquete `database/`
#### `base.py`
- `NAMING_CONVENTION` determinista (`pk_%(table_name)s`, `fk_...`, etc.) + `Base(DeclarativeBase)`; `TimestampMixin` (`created_at/updated_at` server-side); `str_enum(enum_cls, length=16)` → columna `Enum(native_enum=False)` que guarda **valores** ("PENDING").
- **Por qué**: nombres de constraints estables para Alembic; enums VARCHAR portables (sin tipos ENUM de PG que rompen migraciones).

#### `session.py`
- `create_engine(settings)`: `pool_pre_ping=True`, `pool_recycle=1800` (sobrevive desconexiones idle).
- `create_session_factory(engine)`: `expire_on_commit=False`, `autoflush=False` — los casos de uso trabajan con entidades de dominio desprendidas sin sorpresas de lazy-load.
- **Por qué**: configuración de pool/sesión probada para contenedores con reinicios de red.

#### Modelos ORM — `models/`
- `document_model.py` — `documents`: `filename(255)`, `content_type(100)`, `size_bytes BIGINT`, `storage_path(512) UNIQUE`, `document_type/status` (str_enum); índice compuesto `(status, created_at DESC)`.
- `supplier_model.py` — `suppliers`: `name(255)`, `tax_id(64) UNIQUE+INDEX` (dedup por NIT), datos de contacto.
- `invoice_model.py` — `invoices`: FK `document_id UNIQUE CASCADE` (idempotencia), FK `supplier_id RESTRICT`, `uq_invoices_supplier_number`, montos `Numeric(14,2)`, `validation_report/raw_extraction JSONB`; relación `items` con `cascade=all, delete-orphan` ordenada por `position`, `lazy=selectin`; relación real `supplier` para que SQLAlchemy INSERTe suppliers antes de invoices.
  - `invoice_items`: `position INT`, `quantity Numeric(12,3)`, precios `Numeric(14,2)`.
- `job_model.py` — `processing_jobs`: FK documento (CASCADE) e invoice (`SET NULL`), `status/attempts/celery_task_id/error_message/timestamps`; relaciones reales `document`/`invoice_ref` (orden de INSERT garantizado); índice `(status, created_at DESC)`.
- `__init__.py` reexporta los modelos.
- **Por qué cada detalle**: `JSONB` audita el payload crudo del extractor; `selectin` evita N+1; relaciones reales resuelven el orden de inserción entre mappers independientes.

### 4.4 Paquete `repositories/`
- `mappers.py` — traductores entidad↔ORM (capa anti-corrupción): `document_to_domain/apply_document`, `supplier_to_domain/build_supplier_model/apply_supplier`, `invoice_to_domain` (construye ítems ANTES: el `__post_init__` exige ≥1), `build_invoice_models` (agregado→modelos con posiciones), `summary_from_models` (proyección plana para `InvoiceSummary`), `job_to_domain/apply_job`, helpers de fechas.
- `unit_of_work.py` — `SqlAlchemyUnitOfWork(UnitOfWork)`: context manager que abre `Session` y monta los 4 repositorios; `commit()` traduce `IntegrityError` (con diagnóstico psycopg3 y nombre de constraint) a `PersistenceError(no-retryable)` y demás `SQLAlchemyError` a `PersistenceError(retryable)`; rollback automático en excepción.
- `sqlalchemy_document_repository.py` — `add/get/update` (update lanza `PersistenceError` si no existe).
- `sqlalchemy_supplier_repository.py` — `add/get/find_by_tax_id(strip())`.
- `sqlalchemy_job_repository.py` — `add/get/update/count_by_status()` (GROUP BY estado, tolerante a enum ORM o str).
- `sqlalchemy_invoice_repository.py` — `add` (modelo+ítems), `get`, `get_by_document` (más reciente por created_at), `query` (JOIN outer supplier; filtros ILIKE sobre number/name/tax_id + rango issue_date; COUNT sobre subquery; orden issue_date DESC, created_at DESC; offset/limit → `InvoiceListPage`), `stats` (COUNT + SUM total).
- **Por qué**: repositorios finos (toda la traducción vive en mappers), consultas eficientes y errores de integridad con mensaje accionable.

### 4.5 Paquete `llm/` — extracción híbrida
#### `__init__.py`
- `build_invoice_extractor(settings) -> InvoiceExtractor`: fábrica según `LLM_EXECUTION`:
  - siempre envuelve `HybridInvoiceExtractor(RulesExtractor(), fallback)` donde
  - fallback = `OpenAICompatibleExtractor(...)` si `api`, o `LocalOCRInvoiceExtractor(...)` si `local` (100% offline).
- **Por qué**: el interruptor api/local vive en UNA función; el resto del sistema solo ve `InvoiceExtractor`.

#### `rules_extractor.py`
- `RulesInvoiceExtractor.extract(document_text, document_type) -> ExtractedInvoice` — extracción determinista por regex sobre el texto **canonizado** por `normalize_invoice_text()` (mayúsculas sin acentos: los patrones no usan `re.I` ni alternancias de tildes).
- **Número**: lista ordenada de patrones con nombre en el reporte (`referencia_de_pago`, `factura_electronica_venta_no`, `numero_prefijo_guion_linea` "FV - 137", `numero_solo_prefijo_no` "No. 11052"); un guard `_AUTH_CONTEXT_PATTERN` descarta candidatos dentro de frases de autorización/resolución DIAN (numeración de 14 dígitos).
- **Fechas**: emisión y vencimiento por etiqueta (valor en la misma línea o en la siguiente) y formatos dd/mm/aaaa, ISO `aaaa-mm-dd` y dd-mmm-aaaa.
- **Bloque monetario**: subtotal/IVA/total como trío coherente con normalización de miles; patrones priorizados (`total_a_pagar` → … → `total_simple`).
- **Proveedor**, en orden de prioridad: etiqueta `EMISOR:` → línea con sufijo societario (S.A.S/LTDA…, prefiriendo el segmento tras guion "Marca - Marca S.A.S.") → proximidad a NIT (nombre en la misma línea o en la anterior a un NIT desnudo). Los NIT de la plataforma de facturación (Alegra/Facturatech) se excluyen vía `_PLATFORM_LINE_PATTERN`; si no hay NIT impreso se genera placeholder determinista `SIN-NIT-{slug}` para deduplicar.
- Ítems tabulares (cantidad/descripción/cant./precios) vía `_ITEM_ROW_PATTERN`.
- Si faltan campos requeridos lanza `PartialExtractionError(partial_data=..., missing_fields=...)` llevándose lo encontrado.
- **Por qué**: gratis, rápido, auditable y suficiente para facturas electrónicas bien formadas (7/7 del corpus real se resuelven solo con reglas); los parciales alimentan la fusión.

#### `openai_extractor.py`
- `OpenAICompatibleExtractor(api_key, base_url, model, ...)`: cliente OpenAI-compatible (chat completions multimodal) con páginas renderizadas como imágenes base64; instrucción estricta de JSON; parse defensivo a `ExtractedInvoice`; errores → `LLMExtractionError`.
- **Por qué**: máxima precisión en documentos difíciles usando cualquier endpoint compatible (incluye Gemini vía OpenAI-compat).

#### `local_ocr_extractor.py`
- `LocalOCRInvoiceExtractor(ocr_provider, page_renderer)`: modo local = **fusión de dos fuentes** — combina el texto original con el OCR de las páginas renderizadas, vuelve a aplicar reglas sobre ese texto enriquecido y construye el resultado.
- Nota histórica intencional: sustituyó al pipeline Qwen/Ollama retirado.
- **Por qué**: privacidad total y cero coste por llamada; rescata campos cuando el PDF digital tiene texto disperso que la primera pasada de reglas no articuló.

#### `hybrid_extractor.py`
- `HybridInvoiceExtractor(rules, fallback).extract(...)`: intenta reglas; ante `PartialExtractionError` fusiona `partial_data` con el resultado del fallback (el trío subtotal/tax/total viaja como bloque coherente); ante fallo del fallback, devuelve el parcial si sirve.
- **Por qué**: minimiza llamadas caras y maximiza tasa de éxito — la estrategia core del proyecto.

#### `mock_extractor.py`
- Extractor determinista con fixture embebida para desarrollo/offline/tests.
- **Por qué**: probar el pipeline completo sin red ni modelos.

#### `page_renderer.py`
- `render_page_images(content, document_type, max_pages) -> list[PIL.Image]`: PDF → imágenes vía pypdfium2 (respeta `max_pages`), imágenes sueltas → tal cual.
- **Por qué**: los extractores de visión necesitan raster; un punto único controla cuántas páginas se procesan.

#### `text_normalizer.py`
- `normalize_invoice_text(raw)`: forma canónica única para las reglas — NFKD sin marcas combinatorias (sin tildes, `Ñ→N`), limpieza tipográfica (NBSP, comillas, guiones, elipsis), **mayúsculas**, colapso de espacios por línea preservando la estructura de líneas (etiqueta sola + valor debajo) y colapso de letras repetidas por ruido de escáner protegiendo los dígrafos legítimos LL/RR/SS/CC y nunca los dígitos.
- **Por qué**: la calidad del OCR varía; canonizar ANTES de las regex multiplica la tasa de acierto y permite patrones deterministas sin defensas de caso/acentos.

### 4.6 Paquete `ocr/`
#### `engines.py`
- Motores intercambiables detrás de una misma interfaz interna:
  - `TesseractEngine` — CLI tesseract por imagen.
  - `PaddleOCREngine` — pipeline PP-OCR clásico (`ocr()`), colección robusta de líneas.
  - `PaddleOCRVLEngine` — PaddleOCR-VL `.predict()` (visión-lenguaje, ideal escaneos): lectura de resultados **por atributo** mediante `_result_field(page, key)` (getattr→get; fix del bug de colección de caracteres) y `_collect_lines` que acepta `markdown`, dict `markdown_texts` o HTML, devolviendo líneas limpias.
- **Por qué**: tres motores según calidad/recursos; el fix `_result_field` eliminó el fallback silencioso que producía líneas de un solo carácter.

#### `local_ocr.py`
- `LocalOCRProvider(OCRProvider)`: ruta rápida de **texto embebido** para PDFs digitales (pypdf) y delegación al motor elegido (`LOCAL_OCR_ENGINE=vl|paddle|tesseract`) para escaneos/imágenes.
- **Por qué**: no pagar OCR cuando el PDF ya trae texto perfecto; un solo puerto para el dominio.

#### `__init__.py`
- `build_ocr_provider(settings)` — fábrica por `LOCAL_OCR_ENGINE`.

### 4.7 Paquete `storage/`
- `local_storage.py` — `LocalDocumentStorage(base_dir)`: `save(content, document_id, filename) -> storage_path` (subcarpetas por id, escritura atómica), `open(path) -> bytes`.
- `__init__.py` — `build_document_storage(settings)`.
- **Por qué**: puerto `DocumentStorage` implementado en disco; migrar a S3/GCS tocaría solo este paquete.

### 4.8 Paquete `security/`
#### `middleware.py`
- `SecurityHeadersMiddleware(app)`: añade en cada respuesta `Strict-Transport-Security: max-age=31536000; includeSubDomains`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, CSP restrictiva y `Permissions-Policy`.
- `RateLimitMiddleware(app, requests_per_minute=60, burst=10)`: token-bucket por IP cliente (honrando `X-Forwarded-For`), omite `/health`, excede → `429` + `Retry-After: 60`.
- `validate_llm_base_url(url)`: rechaza IPs privadas (10/8, 172.16/12, 192.168/16), loopback/link-local, hosts internos (`localhost`, `host.docker.internal`) y esquemas ≠ http(s) → previene **SSRF** apuntando `LLM_BASE_URL` a servicios internos.
- **Por qué**: endurecer la superficie HTTP (headers, flood) y blindar la configuración de LLM contra SSRF; todo testeable unitariamente.

#### `__init__.py`
- Re-exporta los middlewares y el validador.

### 4.9 `app/infrastructure/logging_setup.py`
- `configure_logging(level, log_format)`: formatter de aplicación con módulo/archivo/función/línea y metadatos estructurados de excepción; modo JSON opcional; usado por API, workers (vía señal `setup_logging`) y tests.
- **Por qué**: logs correlacionables (job_id/task_id) y formato uniforme entre procesos.

---

## 5. Capa Presentación

### 5.1 API — `app/presentation/api/`
#### `main.py`
- Fábrica `create_app()` con la pila de middleware **de afuera hacia adentro**:
  1. `SecurityHeadersMiddleware` (cabeceras duras),
  2. `RateLimitMiddleware(requests_per_minute, burst)` (desde settings),
  3. `CORSMiddleware` (orígenes desde `CORS_ORIGINS`; `"*"` solo por defecto de desarrollo),
  routers bajo `/api/v1` (invoices, jobs, dashboard, health), handlers de excepción registrados.
- **Por qué**: punto único de ensamblaje HTTP; el orden garantiza headers incluso en 429.

#### `deps.py`
- Dependencias FastAPI que llaman al container (`Depends(build_list_invoices_use_case)`, etc.).
- **Por qué**: puente FastAPI→DI sin que los routers conozcan implementaciones.

#### `routers/invoices.py`
- `POST /api/v1/invoices/upload` (multipart) → 202 con ids de documento/job.
- `GET /api/v1/invoices` (query: `search`, `date_from`, `date_to`, `limit`, `offset`) → página de resúmenes.
- `GET /api/v1/invoices/{invoice_id}` → detalle completo.
- **Por qué**: contratos REST mínimos que cubren subir, listar y consultar.

#### `routers/jobs.py`
- `GET /api/v1/jobs/{job_id}` → estado del job (polling del front).
- **Por qué**: el procesamiento es asíncrono; el usuario necesita seguirlo.

#### `routers/dashboard.py`
- `GET /api/v1/dashboard/stats` → métricas agregadas.
- **Por qué**: alimenta tanto al panel web como a integraciones externas.

#### `routers/health.py`
- `GET /health` → liveness/readiness (exento de rate-limit deliberadamente, para probes).

#### `schemas.py`
- Modelos Pydantic de request/response (`InvoiceSummaryResponse`, `InvoiceDetailResponse`, `JobStatusResponse`, `DashboardStatsResponse`, …).
- **Por qué**: validar salida y documentar OpenAPI automáticamente.

#### `mappers.py`
- Conversión entidades/DTOs de aplicación → schemas Pydantic.
- **Por qué**: los routers quedan finos; el contrato HTTP cambia sin tocar la app.

#### `exception_handlers.py`
- Mapea `AppError` → códigos HTTP (404 not_found, 400 invalid_file/business, 413 file_too_large, 503 servicios externos…) con cuerpo `{code, message}` y logging estructurado.
- **Por qué**: respuestas de error consistentes y trazables en toda la API.

### 5.2 UI — `app/presentation/streamlit/`
- `app.py` — shell de la app multi-página (título, navegación, configuración de página).
- `api_client.py` — cliente HTTP tipado hacia la API (`upload_invoice`, `list_invoices`, `get_invoice`, `get_job_status`, `dashboard_stats`); centraliza timeouts y manejo de errores.
- `pages/upload_invoice.py` — formulario de carga (drag&drop, validación cliente, polling del job hasta terminal mostrando avance).
- `pages/invoices.py` — listado con búsqueda y filtros de fecha + paginación.
- `pages/invoice_detail.py` — vista detallada (proveedor, totales, ítems, reporte de validación).
- `pages/dashboard.py` — KPIs y distribución de estados desde `/dashboard/stats`.
- **Por qué**: demo funcional sin escribir JS; toda la lógica reside en la API (la UI es un cliente más).

---

## 6. Configuración

### `app/config/settings.py`
- Clase `Settings` (pydantic-settings, lee `.env`) con grupos:
  - Infra: `database_url`, `redis_url`.
  - Upload: `max_file_size_bytes`.
  - Storage: `storage_backend`, `local_storage_dir`.
  - **LLM**: `llm_execution: Literal["api","local"]` (interruptor principal), `llm_base_url` (validada contra SSRF), `llm_api_key`, `llm_model`, `vision_max_pages`.
  - OCR local: `local_ocr_engine: Literal["vl","paddle","tesseract"]`.
  - Celery: `celery_max_retries`, `celery_retry_backoff_seconds`, `celery_task_timeout_seconds`.
  - Observabilidad: `log_level`, `log_format`.
  - Seguridad: `cors_origins: str = "*"`.
- Validadores: `@field_validator("llm_base_url")` → `_validate_llm_base_url` (rechaza redes privadas/loopback/hosts internos/esquemas inválidos; estructura try/except/**else** para no tragarse su propio error).
- `get_settings()` con `lru_cache`.
- **Por qué**: configuración tipada y validada al arrancar (falla rápido si algo está mal), con valores seguros por defecto.

### `app/config/__init__.py`
- Re-exporta `Settings`, `get_settings`.

---

## 7. Tests

- `conftest.py` — fixtures compartidas: settings de prueba, contenido mínimo PDF/PNG/JPEG, mocks comunes; marcadores `unit/integration/e2e`.
- `fakes.py` — dobles en memoria: `FakeStorage`, `FakeDispatcher`, fake de OCR/extractor según necesidad.
- `fakes_uow.py` — `InMemoryUnitOfWork` + repositorios dict-based que imitan la semántica del UoW real.
- **Unitarios** (`tests/unit/`):
  - `test_money.py` (formatos mixtos, redondeo, negativos), `test_file_type.py` (magic bytes, MIME, sanitización), `test_extracted_schema.py`, `test_rules_extractor.py` (regex sobre OCR sucio), `test_mock_llm.py`, `test_hybrid_vision_extraction.py` (reglas→escalada, fusión de parciales), `test_local_ocr.py` (texto embebido vs motor), `test_text_normalizer.py`, `test_invoice_validator.py`, `test_upload_use_case.py` (rechazos + happy path + dispatch), `test_process_use_case.py` (pipeline completo con fakes, marcado FAILED en errores), `test_logging_setup.py`, `test_exception_handler_logging.py`, además de pruebas de cabeceras/rate-limit/SSRF añadidas con el endurecimiento.
- `tests/integration/test_postgres_repositories.py` — repositorios y UoW reales contra Postgres (marcadas `integration`).
- `tests/e2e/test_live_stack.py` y `test_api_offline.py` — humo end-to-end contra el stack levantado (upload→poll→detalle→dashboard).
- **Por qué**: pirámide inversa al coste — la lógica pura se prueba sin contenedores; Postgres/Celery solo en integración; E2E valida el cableado real en ambos modos `LLM_EXECUTION`.

---

## 8. Migraciones

- `alembic.ini` — configuración (URL inyectada desde settings/env).
- `env.py` — conecta `Base.metadata` (con naming convention) y settings.
- `versions/0001_initial.py` — crea las 5 tablas con constraints/índices nombrados.
- `versions/0002_drop_invoice_validation_status.py` — retira la columna redundante `validation_status` de invoices (el veredicto vive en `validation_report` JSONB).
- **Por qué**: esquema versionado y reproducible; la naming convention hace las autogeneraciones estables.

---

## 9. Raíz del repositorio y scripts

- `Dockerfile` — imagen única Python-slim: instala deps (incl. PaddleOCR/Tesseract), copia código; entrypoint flexible.
- `docker-entrypoint.sh` — arranca según rol (`api`, `worker`, `streamlit`) aplicando migraciones al iniciar.
- `docker-compose.yml` — servicios: `postgres:16-alpine` (volume + healthcheck), `redis:7-alpine`, `api` (uvicorn, puerto expuesto, healthcheck `/health`), `worker` (celery `-Q invoices`, volumen de modelos OCR cacheado), `streamlit`. Red interna, variables desde `.env`.
- `Makefile` — atajos: `up/down/logs/ps`, `lint`, `test`, `migrate`, reconstrucción de imagen.
- `.env` / `.env.example` — configuración por entorno (ejemplo documenta `CORS_ORIGINS=*` y el interruptor `LLM_EXECUTION`).
- `scripts/healthcheck.py` — chequeo ligero usado por el healthcheck del contenedor.
- `docs/manual_tecnico.md`, `docs/manual_usuario_pruebas.md` — decisiones (#1–#13) y guía de pruebas.
- `docs/arquitectura.drawio` — diagrama de arquitectura (actualizado junto a este documento).
- **Por qué**: reproducibilidad con un solo comando (`make up`); el mismo artefacto Docker sirve para los tres roles.

---

*Cierre*: cualquier componente nuevo debe (1) declarar su puerto en dominio/aplicación si introduce una dependencia externa, (2) implementarse en infraestructura, (3) cablearse SOLO en `container.py`, y (4) traer tests unitarios con fakes. Así se mantiene la hexagonal sana.
