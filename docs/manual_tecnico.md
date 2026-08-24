# Manual Técnico — Invoice Processing System

> Sistema de procesamiento asíncrono de facturas: PDF/imagen → OCR → LLM (extracción
> estructurada) → validación de negocio → PostgreSQL, con API REST (FastAPI), workers
> distribuidos (Celery + Redis) e interfaz web (Streamlit).

---

## 1. Visión general

| Aspecto | Valor |
|---|---|
| **Python** | ≥ 3.12 |
| **API** | FastAPI + Uvicorn |
| **Cola de tareas** | Celery 5 + Redis 7 (cola `invoices`) |
| **Base de datos** | PostgreSQL 16 + SQLAlchemy 2.0 + Alembic |
| **OCR** | PyMuPDF (texto embebido) + Tesseract (fallback) |
| **LLM** | Proveedor intercambiable: `mock` (offline) / `openai` |
| **UI** | Streamlit (multi-página) |
| **Tests** | pytest (unit / integration / e2e), fakes en memoria |

### 1.1 Flujo de alto nivel

```
Cliente (curl/UI Streamlit)
   │  POST /api/v1/invoices/upload  (multipart)
   ▼
┌─────────────────────────── FastAPI ───────────────────────────┐
│ UploadInvoiceUseCase:                                         │
│   valida tamaño/tipo → guarda blob → INSERT document+job      │
│   → enqueue Celery (solo tras persistir)                      │
└──────────────┬────────────────────────────────────────────────┘
               │ Redis broker
               ▼
┌─────────────────────────── Worker ────────────────────────────┐
│ process_invoice_task → ProcessInvoiceUseCase:                 │
│   1. job→PROCESSING, document→PROCESSING                      │
│   2. blob → texto (embebido u OCR)                            │
│   3. texto → ExtractedInvoiceData (Pydantic)                  │
│   4. DTO → agregado Invoice (+Supplier dedup por tax_id)      │
│   5. InvoiceBusinessValidator → ValidationReport              │
│   6. persistencia transaccional; job→COMPLETED                │
└───────────────────────────────────────────────────────────────┘
```

---

## 2. Arquitectura hexagonal (puertos y adaptadores)

La regla fundamental: **las dependencias apuntan hacia el dominio**. Nada del
dominio importa FastAPI, SQLAlchemy, Celery o Pydantic (salvo el contrato LLM,
que es un VO de dominio deliberadamente).

```
                    ┌─────────────────────────────────────────┐
                    │              presentation               │
                    │  api/ (FastAPI)      streamlit/ (UI)    │
                    └───────────────┬─────────────────────────┘
                                    │ llama
                    ┌───────────────▼─────────────────────────┐
                    │             application                 │
                    │  use_cases/   services/(puertos app)    │
                    │  dto/                                   │
                    └───────────────┬─────────────────────────┘
                                    │ orquesta
                    ┌───────────────▼─────────────────────────┐
                    │                 domain                  │
                    │ entities/ value_objects/ services/      │
                    │ repositories/(puertos)  exceptions.py   │
                    └───────────────▲─────────────────────────┘
                                    │ implementan los puertos
        ┌───────────────────────────┴───────────────────────────┐
        │                   infrastructure                      │
        │ database/(models,session) repositories/(SQLAlchemy)    │
        │ storage/  ocr/  llm/  celery_app/  container.py        │
        └───────────────────────────────────────────────────────┘
```

* **Puertos (interfaces)**: `domain/repositories/*` (persistencia),
  `domain/services/*` (storage, OCR, extractor, validador),
  `application/services/task_dispatcher.py` (cola).
* **Adaptadores**: implementaciones concretas en `infrastructure/` y los fakes
  de `tests/fakes*.py` — ambos prueban que la arquitectura funciona sin red.
* **Raíz de composición**: `infrastructure/container.py` es el ÚNICO módulo que
  conoce todas las clases concretas. FastAPI (`presentation/api/deps.py`) y
  Celery (`tasks.py`) obtienen sus casos de uso desde ahí.

---

## 3. Módulos, capa por capa

### 3.1 `app/config/settings.py`
Configuración central tipada con `pydantic-settings`: se lee de variables de
entorno / `.env`. Puntos clave:
* `SecretStr` para `llm_api_key` (nunca aparece en logs ni `repr`).
* Validadores: `log_level` ∈ {DEBUG, INFO, WARNING, ERROR}; proveedores en minúsculas.
* Propiedades derivadas: `max_file_size_bytes`, `is_production`.
* `get_settings()` cacheada con `lru_cache` — punto de entrada único.

### 3.2 Capa de dominio (`app/domain/`)

#### `exceptions.py`
Jerarquía única `AppError` con dos metadatos clave: `code` (string estable para
la API) y `retryable: bool` (consumido por la política de reintentos). Errores
de negocio puros: `FileTooLargeError`, `InvalidFileError`, `LLMExtractionError`,
`OCRExtractionError`, `PersistenceError(retryable=...)`, `JobNotFoundError`,
etc.

#### `entities/` — agregados con invariantes
| Entidad | Responsabilidad | Invariantes / máquina de estados |
|---|---|---|
| `Document` | Metadatos del archivo subido | RECEIVED → PROCESSING → PROCESSED \| FAILED (`mark_processing/mark_failed/is_processed`) |
| `ProcessingJob` | Unidad de trabajo asíncrono | PENDING → PROCESSING → COMPLETED\|FAILED; `start()` incrementa `attempts`; no se reinicia un COMPLETED ni se completa un FAILED; `error_message` truncado a 2000 chars |
| `Invoice` (raíz) | Factura extraída + resultado de validación | número requerido, moneda ISO-3 mayúsculas, **≥ 1 línea**, total > 0; `apply_validation(report)` fija VALID/INVALID |
| `InvoiceItem` | Línea de factura | convención **neta**: `total = cantidad × precio_unitario` |
| `Supplier` | Emisor deduplicado por `tax_id` | nombre y tax_id obligatorios |

Los tres primeros son dataclasses `slots=True` con `__post_init__` como
guardián de invariantes (validación de nivel 2).

#### `value_objects/`
* `money.py` — `Money`: Decimal inmutable, cuantizado a 2 decimales
  (ROUND_HALF_UP), nunca negativo. `parse()` tolera formatos europeos
  (`"12,5"`, `"1.234,56"`, `"1,234.56"`); cadenas vacías/inválidas lanzan
  `MoneyError`. `is_close(other, tolerance)` para comparaciones contables.
* `enums.py` — `DocumentType`, `DocumentStatus`, `JobStatus`,
  `InvoiceValidationStatus` (StrEnum; la BD guarda el valor textual).
* `extracted_invoice.py` — `ExtractedInvoiceData` (Pydantic): **el único formato
  aceptado de un LLM**. Validación de nivel 1: fechas coherentes, montos ≥ 0,
  moneda ISO, `items` mínimo 1, coerción numérica ("1.234,56" → Decimal).
* `file_type.py` — `FileType.validate()`: extensión whitelist (pdf/png/jpg/jpeg)
  + MIME declarado + *sniffing* de magic bytes (`%PDF-`, `\x89PNG`, `\xff\xd8\xff`)
  + coherencia extensión↔contenido↔MIME. `sanitize_filename()` neutraliza rutas.
* `validation.py` — `Severity` (ERROR/WARNING/INFO), `ValidationIssue`
  (code+severity+message+field, serializable) y `ValidationReport`
  (`errors`, `warnings`, `is_valid`, `to_dict()`).

#### `services/` — puertos de dominio
`DocumentStorage`, `OCRProvider` (con `OCRResult{text,page_count,method}`),
`InvoiceExtractor`, y el servicio puro `invoice_validator.InvoiceBusinessValidator`
(validación de nivel 3): campos obligatorios, cordura de fechas, rangos
numéricos y aritmética subtotal/IVA/total/líneas con tolerancia configurable
(0.02). Descuadres ≤ tolerancia son WARNING (`math.item_line_rounding`);
fuera de tolerancia son ERROR.

#### `repositories/` — puertos de persistencia
ABCs `DocumentRepository`, `SupplierRepository`, `JobRepository`,
`InvoiceRepository` (+ objetos de consulta `InvoiceQuery`, página
`InvoiceListPage` con `total_count`, y `InvoiceStats` para el dashboard).
`get_by_document(document_id)` existe para garantizar **1 factura por documento**
(idempotencia del pipeline).

### 3.3 Capa de aplicación (`app/application/`)

#### `use_cases/upload_invoice.py`
Entrada `UploadCommand` (dataclass inmutable, agnóstica del transporte) →
salida `UploadResult`. Orden deliberado: tamaño → tipo → guardado de blob →
persistencia (document + job PENDING en una transacción) → **enqueue al final**
(evita trabajos fantasma si la BD falla).

#### `use_cases/process_invoice.py` (corazón del sistema)
Pipeline completo ejecutado por workers. Decisiones importantes:
* **Transacciones cortas**: OCR/LLM corren FUERA de transacciones (sin locks de
  BD esperando servicios externos).
* `_begin()`: guarda de idempotencia — si el job ya está COMPLETED retorna
  sentinel; si existe factura previa para el documento (crash entre commits)
  **resume** enlazándola sin re-procesar.
* `_handle_failure()`: errores `retryable` dejan el job en PROCESSING (Celery
  reintenta); errores permanentes cierran FAILED y marcan el documento antes de
  relanzar. Excepciones inesperadas se envuelven en `DocumentProcessingError`.

#### Resto
`get_invoice`, `list_invoices` (paginado + filtros), `get_job_status`,
`dashboard_stats` — casos de lectura simples sobre UoW.
`services/unit_of_work.py` define el patrón UnitOfWork (context manager con
repos agregados y commit/rollback); `dto/dashboard_stats.py` agrupa métricas.

### 3.4 Infraestructura (`app/infrastructure/`)

| Módulo | Contenido |
|---|---|
| `database/models/*.py` | ORM: `documents`, `suppliers`, `invoices`, `invoice_items`, `processing_jobs`; índices (estado+fechas, tax_id único, `(supplier_id, number)` único) |
| `database/session.py`, `base.py` | engine/session factory, `DeclarativeBase` con naming convention |
| `repositories/sqlalchemy_*.py` + `mappers.py` | Implementación de los puertos; `mappers.py` es la capa anti-corrupción entidad↔modelo; `IntegrityError` → `PersistenceError(retryable=False)` |
| `repositories/unit_of_work.py` | UoW transaccional sobre `sessionmaker` |
| `storage/local_storage.py` | Blob en disco: claves `YYYY/MM/<uuid>/<filename>` (evita dirs gigantes), verificación de path traversal, borrado seguro |
| `ocr/local_ocr.py` | Estrategia "OCR solo cuando hace falta": PDF con texto embebido denso (≥40 chars/página) usa PyMuPDF; páginas escaneadas se rasterizan a 200 DPI → Tesseract (`eng+spa`). Imágenes van directas a Tesseract. Errores → `OCRExtractionError` |
| `llm/mock_extractor.py` | Extractor determinista offline: semilla SHA-256 del texto → PRNG reproducible; catálogo de proveedores/líneas; matemática SIEMPRE consistente (subtotal=Σlíneas netas; total=subtotal+21%) |
| `llm/openai_extractor.py` | Implementación real (OpenAI Structured Outputs) con timeouts y `LLMExtractionError` en fallos |
| `celery_app/app.py` | App Celery (broker/backend Redis, cola `invoices`, límites soft/hard, `task_acks_late`) |
| `celery_app/dispatcher.py` | Adaptador de `TaskDispatcher` → `send_task("process_invoice")` |
| `celery_app/tasks.py` | `process_invoice_task`: adjunta `celery_task_id` al job; reintento exponencial (backoff 30s × 2^n, máx 3); timeout → FAILED; agotados reintentos → FAILED; en permanentes NO relanza (BD = fuente de verdad) |
| `container.py` | Raíz de composición: singletons cacheados (engine, storage, dispatcher, OCR, LLM) + builders frescos por operación (UoW, casos de uso, `build_invoice_query`) |
| `logging_setup.py` | Logging JSON/texto con origen trazable en cada registro (`module`, `file`, `function`, `line`) y metadatos estructurados de excepción (`exception.type`, `python_module`, `origin` = punto donde se lanzó, `traceback`). Instala excepthooks (`sys`/`threading`) para que ninguna excepción no capturada escape sin log. Usado por api, worker (vía señal Celery `setup_logging`, `worker_hijack_root_logger=False`) y streamlit |

### 3.5 Presentación (`app/presentation/`)

#### API REST (`api/`)
* `main.py` — fábrica `create_app()`: CORS abierto (MVP), registro de handlers y routers bajo `/api/v1`.
* `routers/` — `invoices.py` (upload con lectura limitada en streaming,
  listado con filtros, detalle), `jobs.py` (polling de estado),
  `dashboard.py` (contadores), `health.py` (ping BD).
* `schemas.py` — contratos Pydantic de entrada/salida (nunca se exponen entidades).
* `mappers.py` — entidad → response (incluye placeholder si falta supplier).
* `deps.py` — providers de FastAPI que delegan en `container`; **única costura
  necesaria** para tests e2e vía `app.dependency_overrides`.
* `exception_handlers.py` — tabla `AppError → HTTP` (404 not founds, 400 fichero
  inválido, 413 demasiado grande, 502 servicio externo, 500 persistencia);
  envoltura uniforme `{"error": {"code", "message"}}`; 422 de validación
  reformateada; handler global 500 sanitizado.

**Endpoints**

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/api/v1/invoices/upload` | multipart `file`; 202 + `{document_id, job_id, poll_url}` |
| GET | `/api/v1/invoices?search=&validation_status=&date_from=&date_to=&limit=&offset=` | paginado |
| GET | `/api/v1/invoices/{id}` | detalle (items, supplier, reporte validación, raw_extraction) |
| GET | `/api/v1/jobs/{id}` | estado del pipeline (polling) |
| GET | `/api/v1/dashboard/stats` | contadores jobs/facturas + total facturado |
| GET | `/health` | liveness + conectividad BD |

#### UI (`streamlit/`)
Multi-página: `app.py` (shell+navegación), `api_client.py` (cliente HTTP hacia
la API), páginas `upload_invoice`, `invoices`, `invoice_detail`, `dashboard`.

### 3.6 Migraciones (`alembic/`)
`alembic upgrade head` crea el esquema; compose lo ejecuta como servicio one-shot
`migrate` antes de levantar api/worker.

---

## 4. Semántica de errores y reintentos (resumen)

| Fallo | `retryable` | Estado job | Acción Celery |
|---|---|---|---|
| Timeout LLM/OCR, error de red | True | PROCESSING (attempts++) | retry backoff exponencial |
| Reintentos agotados | True | FAILED | marca FAILED y termina |
| Fichero corrupto/ilegible, duplicado BD | False | FAILED (persistido por el use case) | NO relanza |
| Job/documento inexistentes | False | FAILED | NO relanza |
| Crash entre commit de factura y completion | — | resume | enlaza factura existente |

Claves de idempotencia: guardia `status == COMPLETED` al entrar, unicidad
`(supplier_id, number)` en BD y `get_by_document()` para reanudar.

### Trazabilidad de errores (logging)

Todo error queda registrado con su origen exacto — módulo, archivo, función
y línea — tanto en api como en worker y streamlit:

| Evento | Nivel | Origen registrado |
|---|---|---|
| Error de dominio manejado (400/404/422) | WARNING | handler + `error_code`, método/path HTTP, traceback del punto de lanzamiento |
| 5xx inesperado | CRITICAL | handler + traceback completo |
| Validación de request fallida | WARNING | campo/loc que falló + path |
| Tarea Celery muerta sin manejo (`task_failure`) | ERROR | nombre/id/args de la task + traceback |
| Excepción no capturada (main o hilos) | CRITICAL | excepthooks `sys`/`threading` instalados por `configure_logging` |

En formato JSON cada registro lleva `module/file/function/line` y, si hay
excepción, un bloque estructurado `{type, python_module, origin{file,function,
line}, traceback}`. Detalle completo en `logging_setup.py` y
`celery_app/signals.py`.

---

## 5. Modelo de datos

```
documents(id PK, filename, content_type, size_bytes, storage_path,
          document_type, status, created_at, updated_at)

suppliers(id PK, name, tax_id UNIQUE, address, phone, email,
          created_at, updated_at)

invoices(id PK, document_id FK UNIQUE→documents, supplier_id FK→suppliers,
         number, issue_date, due_date, currency,
         subtotal, tax_amount, total, validation_status, validation_report JSONB,
         raw_extraction JSONB, created_at, updated_at,
         UNIQUE(supplier_id, number))

invoice_items(id PK, invoice_id FK CASCADE→invoices, description,
              quantity NUMERIC, unit_price NUMERIC, tax_amount NUMERIC,
              total NUMERIC, position)

processing_jobs(id PK, document_id FK→documents, status, attempts,
                invoice_id FK NULL→invoices, celery_task_id,
                error_message, started_at, finished_at, created_at, updated_at)
```

Dinero se persiste como `Numeric(12,2)`; enums como texto legible.

---

## 6. Configuración (variables principales)

`DATABASE_URL`, `REDIS_URL`, `STORAGE_PATH`, `MAX_FILE_SIZE_MB` (10),
`OCR_PROVIDER` (local), `LLM_PROVIDER` (`mock`|`openai`), `LLM_API_KEY`,
`LLM_MODEL` (gpt-4o-mini), `CELERY_MAX_RETRIES` (3),
`CELERY_RETRY_BACKOFF_SECONDS` (30), `CELERY_TASK_TIMEOUT_SECONDS` (600),
`LOG_LEVEL`, `LOG_FORMAT` (json|text).

---

## 7. Despliegue

```bash
make up          # docker compose up -d --build (postgres, redis, migrate, api, worker, streamlit)
make migrate     # alembic upgrade head
make logs        # logs seguidos
make down        # parar
# Escalar workers horizontalmente:
docker compose up -d --scale worker=4
```

Servicios y puertos: API :8000 · Streamlit :8501 · Postgres :5432 · Redis :6379.
Healthchecks en postgres/redis/api/worker/streamlit; `migrate` gatea el arranque
de api y worker (`service_completed_successfully`).

---

## 8. Estrategia de pruebas

Pirámide completa, **fakes en memoria en lugar de mocks**, lo que demuestra que
los bordes de la arquitectura son reales:

| Suite | Qué prueba | Requisitos | Comando |
|---|---|---|---|
| `tests/unit/` | Dominio puro, casos de uso con fakes (UoW compartido tipo BD, storage/OCR/LLM/dispatcher falsos), OCR local con pytesseract stubbeado, logging trazable (origen en cada registro, metadatos de excepción, excepthooks) y handlers HTTP que registran todo error | nada externo | `pytest -m unit` |
| `tests/integration/` | Repositorios SQLAlchemy contra PostgreSQL real: roundtrips, unicidades, queries, stats | Postgres | `TEST_DATABASE_URL=... pytest -m integration` |
| `tests/e2e/test_api_offline.py` | La API real de punta a punta sustituyendo adaptadores por fakes via `dependency_overrides` + worker simulado inline | nada externo | `pytest -m e2e` |
| `tests/e2e/test_live_stack.py` | Stack completa desplegada (httpx contra :8000, polling del worker Celery) | `RUN_LIVE_E2E=1` + compose arriba | ídem |

Piezas clave de los fakes (`tests/fakes_uow.py`, `tests/fakes.py`):
* `FakeStore` + `FakeUnitOfWork(store)`: varias sesiones sobre el mismo almacén
  = semántica de BD multi-transacción.
* `RecordingDispatcher(inline_handler)`: graba enqueues y opcionalmente ejecuta
  el pipeline síncronamente (simula el worker en e2e).
* `FakeLLM(data=…|exc=…)`, `FakeStorage`, `FakeOCR` deterministas.

Marcadores declarados en `pyproject.toml`; integración/e2e-vivo se saltan solos
cuando sus variables de entorno no están.

---

## 9. Decisiones de diseño destacadas

1. **Fakes > mocks**: las pruebas ejercitan los casos de uso reales; solo se
   simulan los bordes físicos (red, disco, tiempo).
2. **Determinismo total en modo mock**: mismo texto → misma factura (semilla
   SHA-256), ideal para demos y e2e repetibles.
3. **Transacciones cortas**: I/O pesado fuera de tx → escalado horizontal seguro.
4. **Enqueue después de persistir** y **reanudación por factura existente**:
   exactamente-una-factura-por-documento incluso con crashes/redelivery.
5. **Errores con `retryable`**: la política de reintento vive en una sola idea
   que atraviesa use case y tarea Celery.
6. **Convención neta en importes** (`item.total = qty×precio`; `total =
   subtotal + IVA`) compartida por extractor y validador.
7. **`dependency_overrides` como única costura HTTP**: el e2e offline usa la app
   real sin tocar su código.
8. **Observabilidad trazable por diseño**: cada registro de log lleva su origen
   (`module/file/function/line`) y las excepciones su punto de lanzamiento; los
   excepthooks garantizan que ninguna excepción no capturada escape sin log.
   El worker Celery usa el mismo formatter vía señal `setup_logging`
   (`worker_hijack_root_logger=False`) y `task_failure` registra cualquier
   task muerta con traceback.
9. **Relaciones ORM reales para ordenar el flush**: las tablas relacionadas
   solo por `ForeignKey` (sin `relationship()`) **no tienen orden de INSERT
   garantizado** entre mappers independientes, y las relaciones `viewonly=True`
   no generan aristas de dependencia en el unit of work de SQLAlchemy. Por eso
   `InvoiceModel.supplier`, `ProcessingJobModel.document` e
   `invoice_ref` son relaciones normales: hacen determinista el orden
   `documents → suppliers → invoices → invoice_items → processing_jobs` en un
   único commit y evitan violaciones de FK (23503) intermitentes.

10. **Extracción híbrida con Qwen3-VL** (`hybrid_extractor.py`,
   `page_renderer.py`, `openai_extractor.py` multimodal). Flujo: reglas →
   (patrón no hallado) → modelo de visión. El puerto `InvoiceExtractor`
   acepta imágenes opcionales; el adaptador OpenAI-compatible construye
   mensajes multimodales (texto OCR + PNG base64 por página, hasta
   `VISION_MAX_PAGES`). El fallback es opcional en la fábrica: sin
   credenciales (`LLM_API_KEY`/`LLM_BASE_URL`) el híbrido se comporta como
   reglas puras y re-lanza el error original, así el stack arranca offline.
   La salida del modelo pasa siempre por Pydantic (`ExtractedInvoiceData`);
   las reglas de negocio posteriores deciden persistencia o revisión
   (factura INVALID), cumpliendo el diagrama JSON→Pydantic→reglas→PostgreSQL.

## 11. Fusión simétrica y tolerancia de redondeo (2026-08-23)

- **Fusión híbrida simétrica**: se elimina la prioridad del modelo de visión sobre
  los totales. Ahora todo campo hallado por reglas gana en el merge, incluido el
  trío monetario `subtotal/tax/total`, que viaja como **bloque atómico** en
  `PartialExtractionError.partial_data` cuando las reglas encontraron el total
  (preserva la aritmética interna); si no, los tres valores provienen de visión.
- **Tolerancia de validación**: `InvoiceBusinessValidator` usa tolerancia de
  `$5` a nivel factura (absorbe diferencias de redondeo de $1–5 presentes en
  documentos reales, p. ej. FE364: 136135+25866=162001 vs total declarado
  162000). La verificación por línea (`qty × precio`) conserva tolerancia estricta
  de `0.02`.
- **Bug corregido**: `_to_decimal` interpretaba `50.000` (miles es-CO) como
  `50.00`; ahora distingue decimales (1–2 dígitos tras el punto) de miles (3+).

## 12. Retiro de validation_status y endurecimiento 429 (2026-08-23)

- **Campo `validation_status` eliminado** de toda la pila (dominio, ORM,
  repositorios, API, Streamlit) por no aportar valor actual: el resultado
  del negocio vive en `validation_report` (`is_valid` + issues), que se
  conserva como trazabilidad. Migración Alembic `0002` hace el `DROP COLUMN`.
  Dashboard simplificado a totales; filtro/columna "Status" removidos.
- **Backoff para HTTP 429**: el adaptador OpenAI-compatible distingue
  `_RateLimitedError`, respeta `Retry-After` (header o cuerpo
  "retry in Ns") y espera mínimo 30s entre intentos — los ráfagas cortas
  solo quemaban cuota. Test unitario cubre la espera guiada por servidor.
- **Fallback de ítems en visión**: si el modelo devuelve `items: []`
  (documentos sin filas visibles), se sintetiza un ítem agregado igual que
  en reglas, en lugar de rechazar la extracción.
- **Cuota Gemini gratuita**: `gemini-flash-latest` (gemini-3.7-flash) tiene
  **20 req/día** en capa libre (`GenerateRequestsPerDayPerProjectPerModel`).
  Se cambió `LLM_MODEL=gemini-3.5-flash-lite` (cubo propio, verificado 200).

## 13. Retiro de Qwen3-VL/Ollama: escalada local con PaddleOCR-VL (2026-08-23)

- **Por qué se retira Qwen3-VL local (vLLM/Ollama)**: en este host sin GPU el
  modelo razonaba 20–40 min por factura, se truncaba a `num_ctx=4096`
  (perdiendo el JSON final) y devolvía `{}`/contenido vacío; con trabajos
  apilados la latencia superaba 1 h. La vía LLM local resultó inviable en CPU.
- **Reemplazo (`LLM_EXECUTION=local`)**: la escalada del híbrido ejecuta OCR
  real en contenedor y reusa el extractor de reglas existente:
  - `app/infrastructure/ocr/engines.py`: protocolo `OcrEngine` con tres
    implementaciones seleccionables vía `LOCAL_OCR_ENGINE` — `vl`
    (**PaddleOCR-VL**, pipeline PP-DocLayout + reconocedor VL, default),
    `paddle` (PP-OCR det+rec clásico) y `tesseract`. Los modelos se cachean en
    `/home/appuser/.paddlex` (volumen `paddle_models`).
  - `LocalOCRInvoiceExtractor`: corre reglas sobre **dos fuentes** — texto
    embebido del PDF y líneas OCR — y fusiona los parciales (`setdefault`,
    trío monetario atómico). Si la unión cubre lo requerido devuelve un
    `ExtractedInvoiceData` completo (el híbrido lo ve como éxito normal); si
    no, `PartialExtractionError` con el merge, igual que el fallback remoto.
- **Gotchas de integración Paddle**: exige el extra versionado
  `paddlex[ocr]==<misma versión>` (si no, `DependencyError`); las librerías
  del resultado exponen campos como **atributos** (`.markdown`, `.rec_texts`)
  — `.get()` devuelve `None` silenciosamente; el markdown vive bajo la clave
  `markdown_texts`; las tablas llegan como HTML crudo que se trocea a líneas;
  requiere `libgl1 libglib2.0-0 libgomp1` en la imagen.
- **Rendimiento medido**: PaddleOCR-VL ≈ 230–280 s/página en CPU (una vez);
  PP-OCR/Tesseract son segundos pero pierden layout. En modo `api` la misma
  factura tarda ~5 s con Gemini flash-lite.
- **Switch validado E2E en ambos sentidos** (4 documentos): `local` → 4/4
  COMPLETED (Traductores FE364 vía VL: NIT 900755117-8, trio 136134/25866/
  162000, valid=true) y `api` → 4/4 COMPLETED. Cambiar `LLM_EXECUTION` +
  recrear stack es suficiente; puerto, casos de uso y tests de dominio no
  cambian (arquitectura hexagonal intacta).
