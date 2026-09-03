# Capa Infraestructura — Detalle clase por clase

Complemento del §4 de `desglose_componentes.md` (`app/infrastructure/`). Aquí viven los adaptadores concretos: Celery, SQLAlchemy/P PostgreSQL, OCR, extractores, storage, seguridad y logging. Misma lógica que `capa_dominio_detallada.md`.

---

## Índice

1. [`container.py` — Raíz de composición](#1-containerpy--raíz-de-composición)
2. [`logging_setup.py`](#2-logging_setuppy)
3. [Paquete `celery_app/`](#3-paquete-celery_app)
4. [Paquete `database/`](#4-paquete-database)
5. [Paquete `repositories/`](#5-paquete-repositories)
6. [Paquete `llm/` — extracción híbrida](#6-paquete-llm--extracción-híbrida)
7. [Paquete `ocr/`](#7-paquete-ocr)
8. [Paquete `storage/`](#8-paquete-storage)
9. [Paquete `security/`](#9-paquete-security)

---

## 1. `container.py` — Raíz de composición

Único módulo que conoce todas las clases concretas; API y workers importan de aquí para tener el cableado idéntico.

Funciones fábrica:
- `get_app_settings() -> Settings` — Acceso a configuración.
- `get_engine()` / `get_session_factory()` *(cached con `lru_cache`)* — Motor SQLAlchemy y sessionmaker como singletons por proceso.
- `build_uow() -> SqlAlchemyUnitOfWork` — UoW **fresco por operación** (seguro entre hilos).
- Adaptadores singleton (cacheados): `get_document_storage()`, `get_task_dispatcher()` (`CeleryTaskDispatcher(celery_app)`), `_get_ocr_provider()` (por `OCR_PROVIDER`), `_get_invoice_extractor()` (por `LLM_PROVIDER`/`LLM_EXECUTION`), `_get_invoice_validator()` (`InvoiceBusinessValidator()`).
- Casos de uso frescos por llamada: `build_upload_invoice_use_case()`, `build_process_invoice_use_case()` (conecta OCR + extractor + validador + renderer limitado a `vision_max_pages`), y los 4 de consulta.
- `build_invoice_query(**kwargs) -> InvoiceQuery` — Construcción cómoda del objeto de filtrado.

## 2. `logging_setup.py`

#### `class JsonFormatter(logging.Formatter)`
Formateador JSON sin dependencias. Cada registro lleva timestamp ISO, level, logger, archivo relativo al proyecto (`_short_path`), función, línea y mensaje; cualquier campo del `extra=` serializable se incrusta; las excepciones se estructuran con tipo, módulo Python y **sitio exacto del raise** (`_raise_site`: recorre el traceback hasta el frame más profundo).

Funciones:
- `_short_path(path)` — Ruta relativa a la raíz del proyecto cuando es posible.
- `_raise_site(exc_info)` — `{file, function, line}` donde nació la excepción.
- `_install_excepthooks()` — Instala (una sola vez) `sys.excepthook` y `threading.excepthook` para que ninguna excepción no capturada escape sin loguear (KeyboardInterrupt pasa al hook nativo).
- `configure_logging(level="INFO", log_format="json")` — Setup idempotente del root logger usado por api/worker/streamlit; silencia ruido de httpx/httpcore.

## 3. Paquete `celery_app/`

### `app.py`
Crea el objeto global `celery_app = Celery("invoice_processor", broker/backend=redis_url)` y fija la política operacional en `conf.update(...)`: cola única `invoices`, `task_acks_late=True` (redelivery si un worker muere), `worker_prefetch_multiplier=1` (reparto justo), límites duro/blando desde settings, reintentos de conexión al broker, `result_expires=3600`, UTC, y `worker_hijack_root_logger=False` (nuestro formatter manda). Importa `signals` para registrar los handlers.

### `dispatcher.py`
#### `class CeleryTaskDispatcher(TaskDispatcher)`
Adaptador del puerto de despacho.
- `__init__(celery_application)` — Inyección del app Celery.
- `dispatch_invoice_processing(job_id)` — Normaliza a UUID, hace `send_task("process_invoice", args=[str(job_uuid)], queue="invoices")`; ante `OperationalError/KombuTimeoutError/ConnectionError` lanza `ExternalServiceError("Broker unavailable")`. Loguea job_id + task_id devuelto.

### `tasks.py`
#### `@celery_app.task(bind=True, name="process_invoice", max_retries=settings.celery_max_retries) def process_invoice_task(self, job_id)`
Entrada ejecutada por workers:
1. Log "Task received" con retries; `_attach_celery_task_id(job_id, self.request.id)` para correlación.
2. Construye el use case fresco (`build_process_invoice_use_case()`) y lo ejecuta.
3. `SoftTimeLimitExceeded` → `_mark_job_failed(...timeout...)` y devuelve FAILED/reason timeout.
4. `AppError` → `_handle_failure`.
5. Éxito → `{"status": "COMPLETED"}`.

Auxiliares de módulo:
- `_handle_failure(task, exc, ctx)` — Transitorio con intentos disponibles → `task.retry(countdown=backoff * 2^n)` (backoff exponencial). Permanente → solo reporta `{"status":"FAILED","reason":code}` (la BD ya quedó marcada por el use case; el broker no acumula basura). Reintentos agotados → `_mark_job_failed` y reason `retries_exhausted`.
- `_attach_celery_task_id(job_id, task_id)` — Guarda el id Celery en la fila del job (best-effort).
- `_mark_job_failed(job_id, message)` — Cierra job FAILED + documento FAILED dentro de UoW protegido.

### `signals.py`
Hooks Celery para trazabilidad total:
- `_setup_celery_logging` conectado a `setup_logging` — Los workers usan nuestro formatter (no el secuestro default de Celery).
- `_log_task_failure` conectado a `task_failure` — Toda task muerta con excepción no manejada se loguea a ERROR con nombre/id de task, args y traceback completo.

---

## 4. Paquete `database/`

### `base.py`
- `NAMING_CONVENTION` — Nombres deterministas para índices/constraints (`fk_%(table)s_%(column)s_%(referred_table)s`...): migraciones estables.
- `class Base(DeclarativeBase)` — Base declarativa con esa convención de metadata.
- `class TimestampMixin` — Columnas compartidas `created_at`/`updated_at` (`DateTime(timezone=True)`, default y onupdate a nivel servidor).
- `str_enum(enum_cls, *, length=16)` — Columna `Enum` respaldada por **VARCHAR** (`native_enum=False`, guarda los *valores* "PENDING"/"PDF"): portable y amigable con migraciones.

### `session.py`
- `create_engine(settings)` — Motor psycopg3 con `pool_pre_ping=True` (sobrevive desconexiones idle) y `pool_recycle=1800`.
- `create_session_factory(engine)` — `sessionmaker(expire_on_commit=False, autoflush=False)`: los objetos ORM siguen usables tras el commit (los use cases trabajan con entidades desprendidas).

### Modelos ORM — `models/`

#### `class DocumentModel(Base, TimestampMixin)` — tabla `documents`
Columnas: `id` UUID pk · `filename` (255) · `content_type` (100) · `size_bytes` BigInteger · `storage_path` (512, **unique**) · `document_type` y `status` como enums-varchar. Índice compuesto `(status, created_at DESC)` para colas/listados.

#### `class SupplierModel(Base, TimestampMixin)` — tabla `suppliers`
`id` · `name` (255) · `tax_id` (64, **unique** + index → deduplicación física) · address/phone/email opcionales.

#### `class InvoiceModel(Base, TimestampMixin)` — tabla `invoices`
`id` · `document_id` FK→documents **unique** + CASCADE (ancla de idempotencia: una factura por documento) · `supplier_id` FK RESTRICT · `number` (100), con `UniqueConstraint(supplier_id, number)` · `issue_date` indexada · `due_date` · `currency`(3) · montos `Numeric(14,2)` · `validation_report` y `raw_extraction` como JSONB. Relaciones: `items` (cascade delete-orphan, ordenadas por `position`, carga `selectin`) y `supplier` real (no viewonly) para forzar el orden INSERT suppliers→invoices.

#### `class InvoiceItemModel(Base)` — tabla `invoice_items`
`id` · `invoice_id` FK CASCADE · `position` (orden original del documento) · `description` Text · cantidades/precios `Numeric(12,3)`/`Numeric(14,2)` · relación inversa `invoice`.

#### `class ProcessingJobModel(Base, TimestampMixin)` — tabla `processing_jobs`
`id` · `document_id` FK CASCADE · `invoice_id` FK SET NULL · relaciones reales `document`/`invoice_ref` (bordes explícitos de unit-of-work: garantizan INSERT padre antes que job) · `status` enum-varchar default PENDING · `attempts` · `celery_task_id`(64) · `error_message` Text · `started_at`/`finished_at`. Índice `(status, created_at DESC)`.

`models/__init__.py` re-exporta los cinco modelos.

---

## 5. Paquete `repositories/`

### `mappers.py` — Capa anticorrupción
Traductores puros ORM↔dominio; los repositorios quedan finos y las entidades jamás ven persistencia:
- Documento: `document_to_domain(model)` / `apply_document(entity, model)`.
- Proveedor: `supplier_to_domain` / `build_supplier_model` / `apply_supplier`.
- Factura+ítems: `invoice_to_domain` (construye ítems ANTES: la entidad exige ≥1; envuelve montos en `Money(Decimal(...))`), `build_invoice_models(invoice) -> (InvoiceModel, list[InvoiceItemModel])` (asigna `position` por enumerate), `summary_from_models(model) -> dict` (proyección plana para `InvoiceSummary`, tolerante a supplier ausente).
- Job: `job_to_domain` / `apply_job`.
- Helpers `_as_date` / `_as_optional_date`: normaliza datetime de BD a `date`.

### `unit_of_work.py`
#### `class SqlAlchemyUnitOfWork(UnitOfWork)`
Implementación SQLAlchemy del puerto transaccional.
- `__init__(session_factory)` — Sesión y 4 repositorios en None.
- `__enter__()` — Abre sesión e instancia los cuatro repositorios sobre ella.
- `__exit__(...)` — Rollback si hubo excepción; cierra sesión siempre.
- `commit()` — Confirma; traduce errores de BD al dominio: `IntegrityError` → `PersistenceError(retryable=False)` enriquecido con SQLSTATE y nombre del constraint de psycopg3 (**los duplicados no sanean reintentando**); cualquier otro `SQLAlchemyError` → `PersistenceError` transitorio (reintento vale).
- `rollback()` — Rollback defensivo si hay sesión.

### Repositorios SQLAlchemy

#### `class SqlAlchemyInvoiceRepository(InvoiceRepository)`
- `add(invoice)` — Construye modelos vía mapper y los agrega a la sesión.
- `get(id)` / `get_by_document(document_id)` — Lectura + mapeo a agregado (el segundo, más nuevo por created_at).
- `query(criteria) -> InvoiceListPage` — SELECT con JOIN outer a supplier; filtros `_filters` (search ILIKE sobre number/name/tax_id, rango issue_date); COUNT sobre subconsulta filtrada para `total_count`; orden `issue_date DESC, created_at DESC`; offset/limit; proyecta cada fila a `InvoiceSummary`.
- `stats() -> InvoiceStats` — `COUNT(*)` + `COALESCE(SUM(total), 0)` en una sola pasada.
- `_find_model_by_document` / `_filters` *(helpers)*.

#### `class SqlAlchemyJobRepository(JobRepository)`
- `add(job)` / `get(job_id)` — CRUD básico con mappers.
- `update(job)` — Trae el modelo y le aplica `apply_job`; inexistente → `PersistenceError`.
- `count_by_status() -> dict[str,int]` — GROUP BY status (tolera valores enum o str).

#### `class SqlAlchemyDocumentRepository(DocumentRepository)`
- `add(document)` / `get(document_id)` / `update(document)` — Mismo patrón; update inexistente → `PersistenceError`.

#### `class SqlAlchemySupplierRepository(SupplierRepository)`
- `add(supplier) -> Supplier` — Registra y devuelve el mismo agregado (el id lo generó el dominio).
- `get(supplier_id)` — Por PK.
- `find_by_tax_id(tax_id)` — Búsqueda exacta con `.strip()`; base de la deduplicación de emisores.

---

## 6. Paquete `llm/` — extracción híbrida

### `__init__.py` — Fábrica / punto de intercambio
- `build_invoice_extractor(settings) -> InvoiceExtractor` — Raíz de composición del puerto de extracción según `LLM_PROVIDER`:
  - `mock` → `MockInvoiceExtractor` (avisa en logs que la salida es sintética).
  - `rules`/`local` → `RulesInvoiceExtractor` puro offline.
  - `openai` → siempre visión remota.
  - `hybrid` *(default)* → `HybridInvoiceExtractor(primary=reglas, fallback=...)`; el fallback lo decide el switch **`LLM_EXECUTION`**: `local` → `_build_local_fallback` (OCR + reglas, sin red); `api` → visión remota solo si hay credenciales, si no degrada a reglas planas.
  - Proveedor desconocido → `ConfigurationError`.
- `_build_api_vision(settings)` — Valida credenciales (`LLM_API_KEY`/`LLM_BASE_URL`, si no `ConfigurationError`) y construye `OpenAICompatibleInvoiceExtractor`.
- `_build_local_fallback(settings)` — Construye el motor OCR elegido (`LOCAL_OCR_ENGINE=vl|paddle|tesseract`, idioma `LOCAL_OCR_LANG`) y lo inyecta en `LocalOCRInvoiceExtractor`.

### `rules_extractor.py`
#### `class RulesInvoiceExtractor(InvoiceExtractor)` — parser determinista regex
Trabaja sobre el texto CANONIZADO por `text_normalizer` (mayúsculas sin acentos: patrones sin `re.I` ni tildes). Constantes clave: `_NUMBER_PATTERNS` (lista ordenada con nombre: referencia_de_pago, factura_electronica_venta_no, generico_factura_no, numero_prefijo_guion_linea "FV - 137", numero_solo_prefijo_no "No. 11052"), `_AUTH_CONTEXT_PATTERN` (guard que descarta números dentro de frases DIAN de autorización/resolución/numeración), estrategias de proveedor (`_EMITOR_PATTERN` → línea-empresa `_COMPANY_LINE_PATTERN` → proximidad a NIT `_NIT_IN_LINE_PATTERN` + `_name_from_nit_prefix`/`_name_above_nit`, excluyendo NITs de plataforma con `_PLATFORM_LINE_PATTERN`), matchers de fechas por etiqueta (`fecha_de_emision`, `fecha_de_generacion`, ...) y formatos dd/mm/yyyy, ISO, dd-mmm-yyyy; trio monetario priorizado (`total_a_pagar`→…→`total_simple`) e `_ITEM_ROW_PATTERN` tabular.

Métodos:
- `extract(document_text, images=None) -> ExtractedInvoiceData` — Detecta contexto colombiano (`_COLOMBIAN_MARKERS`), corre número/proveedor/fechas/montos/ítems acumulando un reporte por campo `{patron_usado}`; si faltan requeridos (`number/supplier/issue_date/total`) lanza `PartialExtractionError(partial_data, missing_fields)`; sin nada útil → `LLMExtractionError`. Loguea el reporte completo.
- Auxiliares privados: `_find_number` (+`_in_authorization_context`), `_find_supplier`, `_parse_date`, `_labelled_date` (+`_find_issue_date`/`_find_due_date`), `_find_amount`, `_find_currency` (ISO explícita o heurística colombiana), `_parse_items`, `_record`/`_log_report`.
- Funciones de módulo: `_to_decimal(raw)` (miles europeos/mixtos → Decimal) y `ensure_items_payload(payload)` (garantiza lista de ítems sintetizable cuando el modelo omitió renglones).

### `hybrid_extractor.py`
#### `class HybridInvoiceExtractor(InvoiceExtractor)`
Orquestador reglas→escalada.
- `__init__(primary, fallback=None)` — El fallback es opcional: sin credenciales de visión el híbrido se comporta como reglas puras.
- `extract(document_text, images=None)`:
  - Reglas OK → devuelve directo (cero llamadas caras).
  - `PartialExtractionError` → escala al fallback y hace `_merge`: **todo campo hallado por reglas gana** (el trío subtotal/tax/total viaja como bloque atómico); el modelo de visión llena huecos. Sin fallback configurado → re-lanza con aviso accionable.
  - `LLMExtractionError` (nada encontrado) → visión al 100%.
- `_merge(partial, vision) -> (from_rules, ExtractedInvoiceData)` *(staticmethod)* — Fusiona dicts y revalida con Pydantic.

### `local_ocr_extractor.py`
#### `class LocalOCRInvoiceExtractor(InvoiceExtractor)`
Modo local sin LLM: OCR sobre páginas renderizadas + el MISMO parser de reglas.
- `__init__(engine: OcrEngine)` — Motor inyectado; crea internamente su `RulesInvoiceExtractor`.
- `extract(document_text, images=None)` — Sin imágenes → `LLMExtractionError` (no se pudo rasterizar). Con ellas: `_ocr_text()` transcribe página a página y `_fuse()` combina fuentes.
- `_ocr_text(images)` — Acumula líneas del motor con log por página.
- `_fuse(embedded_text, ocr_text) -> ExtractedInvoiceData` — Estrategia dual-source: corre reglas sobre texto embebido Y sobre OCR; resultado completo en cualquiera → devuelto tal cual. Parciales → `_merge_partials` (unión campo a campo; trío monetario atómico propiedad de quien halló el total). Si la unión no cubre el esquema Pydantic → `PartialExtractionError` con los campos recuperables (contrato idéntico al resto). Sin campos en ninguna fuente → `LLMExtractionError`.
- `_merge_partials(partials)` *(staticmethod)* — `setdefault` por campo ignorando vacíos + override atómico del bloque monetario.

### `openai_extractor.py`
#### `class OpenAICompatibleInvoiceExtractor(InvoiceExtractor)`
Adaptador visión contra cualquier endpoint OpenAI-compatible (Gemini vía compat, OpenAI, vLLM...).
Constantes: `_MAX_TEXT_CHARS=100k` (guard de prompt), `_RETRYABLE_STATUS={408,409,429,500,502,503,504}`, backoff cap 8s, mínimo 30s ante rate-limit, `_FENCE_RE` (quita ```json), `_RETRY_IN_RE` (lee "retry in Xs"), y `_SYSTEM_PROMPT` con el contrato JSON exacto + reglas anti-alucinación (formatos monetarios colombianos "$162.000"=162000, item.total NETO, subtotal=Σitems, total=subtotal+tax, "omite lo que no puedas leer").
- `__init__(*, api_key, model, base_url=None, timeout_seconds, temperature, max_attempts, transport=None)` — Cliente httpx con Bearer solo si hay key (gateways self-hosted legítimos van sin ella); `transport` es costura para tests.
- `extract(document_text, images=None)` — Hasta `max_attempts`: llama, parsea y valida; reintentos con backoff exponencial (429 espera ≥30s o el Retry-After del servidor); JSON/schema inválidos también reintentan; agotados → `LLMExtractionError` resumida. La API key jamás aparece en logs.
- `_request_chat(prompt, images)` — POST `/chat/completions`; degrada JSON-mode si el backend rechaza `response_format`; 429 → `_RateLimitedError`; status reintentable → `_RetryableError`; 4xx definitivos → `LLMExtractionError`; respeta el campo `reasoning` cuando `content` viene vacío (modelos thinking).
- `_build_body(...)` — Mensaje multimodal (texto + imágenes PNG base64 `detail: high`); sin imágenes usa content string plano por compatibilidad.
- `_parse_json(content)` — Quita fences y, si hay prosa alrededor, recupera el objeto más externo.
- `_retry_after_seconds(response)` / `_backoff(attempt)` — Cortesías de reintento.
- Errores internos: `_RetryableError` (transporte/servidor) y `_RateLimitedError(_RetryableError)` con `retry_after`.

### `mock_extractor.py`
#### `class MockInvoiceExtractor(InvoiceExtractor)`
Determinista: semilla SHA-256 del texto → proveedor de catálogo (`_SUPPLIERS`), 1–4 ítems de `_ITEM_CATALOG`, IVA 21% consistente (subtotal=Σlíneas netas, total=subtotal+tax), fechas relativas. Para demos/e2e sin red ni coste.

### `page_renderer.py`
- `render_page_images(content, document_type, max_pages=4) -> list[bytes]` — PDF → PNGs vía PyMuPDF (`_ZOOM=1.5` ≈108dpi, hasta max_pages); imágenes → pasan tal cual. **Nunca lanza**: PDF corrupto/lib nativa ausente → warning y `[]` (el extractor sigue solo-texto).

### `text_normalizer.py`
- `normalize_invoice_text(raw) -> str` — Forma canónica única para las reglas: (1) NFKD sin marcas combinatorias (sin tildes, Ñ→N); (2) limpieza tipográfica (NBSP, comillas curvas, guiones, elipsis); (3) mayúsculas; (4) colapso de espacios por línea preservando estructura de líneas (etiqueta sola + valor debajo); (5) colapso de rachas de letras repetidas del escáner protegiendo dígrafos LL/RR/SS/CC y jamás dígitos. Ver detalle en §2.10 del desglose y tests unitarios.

---

## 7. Paquete `ocr/`

### `engines.py` — Motores de OCR intercambiables

#### `class OcrEngine(Protocol)`
Contrato mínimo: `lines(image_bytes) -> list[str]` (texto en orden de lectura). Cualquier implementación que lo satisfaga encaja sin tocar dominio/aplicación.

#### `class TesseractLinesEngine`
Motor clásico vía pytesseract + PIL; cero dependencias extra.
- `__init__(language="eng+spa")`; `lines(...)` — OCR de la imagen y líneas no-vacías; fallo → `OCRExtractionError`.

#### `class PaddleOCREngine`
PP-OCR (det+rec) clásico. Inicialización **lazy** (`_engine()`): mantiene paddle fuera del path api/streamlit; kwargs tolerantes a versión (`use_doc_orientation_classify=False`, etc., con fallback a constructor simple).
- `lines(image_bytes)` — PIL→numpy, `predict()` (o `ocr()` en paddleocr<3.0) y `_collect_lines`.
- `_collect_lines(result)` *(staticmethod)* — Aplana las formas conocidas de salida: v3 dict-like con `rec_texts`; v2 pares `[caja,(texto,conf)]`.
- `_result_field(page, key)` — Lee campos por atributo y cae a `.get()`: los resultados de PaddleOCR "mienten" con .get(); fix del bug de caracteres sueltos.

#### `class PaddleOCRVLEngine`
Pipeline documental PaddleOCR-VL (PP-DocLayout + PP-OCRv5 + reconocedor visión-lenguaje 0.9B). Lazy igual que el anterior.
- `lines(image_bytes)` — predict → `_collect_lines`.
- `_collect_lines(result)` *(staticmethod)* — Extrae texto del markdown del pipeline: si es dict usa `markdown_texts`; convierte tablas HTML en líneas (los regex de reglas ven cada celda: `$136.135,`...); filtra separadores `---`; páginas sin markdown caen al collector del motor clásico.

- `build_ocr_engine(engine_name, language)` — Fábrica `vl|paddle|tesseract` → instancia; nombre inválido → ValueError.

### `local_ocr.py`
#### `class LocalOCRProvider(OCRProvider)`
Estrategia "**OCR solo cuando hace falta**":
- `__init__(*, language="eng+spa", min_text_chars_per_page=40)`.
- `extract_text(content, document_type)` — PDF → `_extract_pdf`; imagen → `_extract_image`; cualquier excepción de librería se envuelve en `OCRExtractionError` (las del dominio pasan tal cual).
- `_extract_pdf(content)` — Por página: texto embebido PyMuPDF; si ≥ min_chars lo usa (`embedded-text`); si la capa es fina (escaneo) rasteriza a 200 DPI y va a Tesseract. Devuelve `OCRResult` con páginas unidas por `\n\n` y método etiquetado (`embedded-text+tesseract (ocr_pages=N)`): trazabilidad de qué hizo cada página.
- `_extract_image(content)` — Tesseract directo, 1 página.
- `_tesseract(image_bytes)` — Wrapper con error específico si falta el binario.

### `ocr/__init__.py`
- `build_ocr_provider(settings)` — Fábrica del puerto: `local` → `LocalOCRProvider(language=settings.ocr_language, min_text_chars_per_page=settings.ocr_min_text_chars_per_page)`; otro valor → `ConfigurationError`. Punto de extensión para OCR en nube (Textract/Azure/Google).

---

## 8. Paquete `storage/`

#### `class LocalDocumentStorage(DocumentStorage)` (`local_storage.py`)
Blob storage en filesystem con árbol de directorios por fecha (`YYYY/MM/id__nombre`), punto de swap futuro a S3.
- `__init__(root: Path)` — Resuelve y ancla la raíz.
- `save(document_id, filename, content) -> str` — Sanea el nombre (`_SAFE_NAME_RE`: solo `[A-Za-z0-9._-]`, máx 150), construye la clave con fecha, escribe **atómicamente** (tempfile en el mismo directorio + `os.replace`) y devuelve la clave; OSError → `StorageError`.
- `get(storage_key) -> bytes` — Anti-traversal + lectura; clave ausente → `StorageError(retryable=False)` (reintentar no crea el archivo); OSError → `StorageError`.
- `delete(storage_key)` — Best-effort por contrato: faltantes no levantan error, solo warning.
- `_resolve(key)` — Resuelve la ruta y exige que quede DENTRO de la raíz (`is_relative_to`): segunda barrera contra path traversal.
- Constante `_SAFE_NAME_RE`.

---

## 9. Paquete `security/`

(`middleware.py`; re-exportado por `security/__init__.py`.)

#### `@dataclass class _ClientBucket`
Estado del token-bucket por cliente: `tokens` (float) y `last_update`.

#### `class SecurityHeadersMiddleware(BaseHTTPMiddleware)`
Añade a TODAS las respuestas: HSTS (max-age configurable, includeSubDomains), `X-Frame-Options: DENY` (clickjacking), `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, CSP restrictiva (permite inline solo para la docs de FastAPI, `frame-ancestors 'none'`) y `Permissions-Policy` que desactiva geolocalización/micrófono/cámara/pagos.
- `dispatch(request, call_next)` — Envuelve la respuesta e inyecta headers.

#### `class RateLimitMiddleware(BaseHTTPMiddleware)`
Token-bucket **por IP, en memoria** (adecuado para single-container; documentado el límite y la alternativa Redis para multi-container).
- `__init__(app, *, requests_per_minute=60, burst=10)` — Convierte rpm a tokens/segundo; cubetas bajo `Lock` (thread-safe).
- `_client_key(request)` — Honra `X-Forwarded-For` tras proxy; si no, host del cliente.
- `_consume(key, tokens=1)` — Rellena proporcional al tiempo transcurrido (cap = burst), descuenta y decide; todo atómico bajo lock.
- `dispatch(...)` — Omite `/health` (probes infinitas no deben agotar cuota); sin tokens → `429` con cuerpo `{"detail": "Rate limit exceeded"}` y `Retry-After: 60`.

#### SSRF
- `_is_private_ip(url)` — Verdadero si el host es IP privada/loopback/link-local o hostname interno (`localhost`, `host.docker.internal`).
- `validate_llm_base_url(base_url)` — Validación anti-SSRF de `LLM_BASE_URL`: exige esquema http(s) y rechaza objetivos internos; la usa también el validador de campo de `Settings` (fail-fast al arrancar).
