# Exploración de archivos — Invoice Processing System

> Documento que recorre **cada archivo del proyecto**, módulo por módulo,
> explicando su propósito y el motivo de cada importación. Complementa a
> `manual_tecnico.md` (arquitectura) y `archivo_estudio.md` (repaso).

---

# Raíz del proyecto

## Makefile
Atajos de desarrollo: `install`, `lint`, `fmt`, `typecheck`, `test`,
`test-unit`, `test-integration`, `test-e2e`, `up`, `down`, `logs`, `ps`,
`migrate`, `api`, `worker`, `ui`. Existe para que nadie memorice comandos de
docker/pytest/alembic.

## pyproject.toml
Fuente única de metadatos: dependencias acotadas por rangos (`fastapi>=0.115,<1`…),
extras `dev` (pytest, ruff, mypy), configuración de pytest (markers
`unit/integration/e2e`, `--strict-markers`) y de ruff (línea 100, py312).
Se usa `setuptools` como backend de build.

## alembic.ini
Configuración de Alembic: apunta la URL de BD al entorno y fija
`script_location = alembic`. Lo consumen `make migrate` y el servicio one-shot
`migrate` de compose.

## docker-compose.yml
Define 6 servicios — `postgres` (16-alpine con healthcheck), `redis`
(7-alpine, AOF activado), `migrate` (alembic upgrade head, corre una vez),
`api` (uvicorn :8000, depende de migrate OK), `worker` (cola `invoices`,
concurrencia configurable, escalable con `--scale worker=4`) y `streamlit`
(:8501). El ancla YAML `x-app-environment` comparte las variables de entorno.
Tres volúmenes: `pgdata`, `redisdata`, `uploads`.

## Dockerfile
Imagen única para api/worker/streamlit/migrate: instala el paquete con pip,
copia el código y no embebe secretos (todo llega por entorno).

## docs/
Documentación viva: `manual_tecnico.md`, `archivo_estudio.md`,
`exploracion_archivos.md` (este documento).

---

# Módulo App

## app/\_\_init\_\_.py
Sin imports. Define `__version__ = "0.1.0"`; `main.py` lo lee para exponerlo
en los metadatos de FastAPI/OpenAPI.

## Config

### app/config/\_\_init\_\_.py
Reexporta `Settings` y `get_settings` desde `settings.py` para que los
consumidores escriban `from app.config import get_settings`.

### app/config/settings.py
Configuración central tipada (todas las variables `.env`). Imports:
- importa `lru_cache` desde functools al inicio del módulo para cachear
  `get_settings()` — la configuración se construye una sola vez por proceso.
- importa `Path` desde pathlib para tipar `storage_path` como ruta real
  (`.resolve()`, `/` portable) en lugar de string crudo.
- importa `Field`, `SecretStr`, `field_validator` desde pydantic para
  declarar reglas (`llm_temperature` acotada 0–2), envolver `LLM_API_KEY` en
  `SecretStr` (nunca aparece en logs/repr) y validar dominios
  (`log_level` ∈ {DEBUG…ERROR}, minúsculas en proveedores).
- importa `BaseSettings`, `SettingsConfigDict` desde pydantic_settings para
  leer variables de entorno / archivo `.env` automáticamente
  (`case_sensitive=False`, `extra="ignore"`).

---

## Domain (el corazón sin dependencias externas)

### app/domain/\_\_init\_\_.py
Vacío a propósito: el paquete dominio no debe arrastrar nada ajeno a él.

### app/domain/exceptions.py
Sin ningún import: jerarquía de errores 100% autónoma (`AppError` base con
`code` + `retryable`; derivados `FileTooLargeError`, `InvalidFileError`,
`EmptyFileError`, `LLMExtractionError`, `OCRExtractionError`,
`StorageError`, `PersistenceError`, `JobNotFoundError`,
`InvoiceNotFoundError`, `DocumentNotFoundError`, `EntityValidationError`,
`BusinessValidationError`, `ConfigurationError`, `ExternalServiceError`,
`DocumentProcessingError`). Que no importe nada garantiza que cualquier capa
puede lanzar/capturar estos errores sin ciclos.

### Entities (agregados con invariantes)

#### app/domain/entities/\_\_init\_\_.py
Reexporta `Document`, `Invoice`, `InvoiceItem`, `Supplier`, `ProcessingJob`.

#### app/domain/entities/document.py
Entidad del fichero subido. Imports:
- importa `dataclass`, `field` desde dataclasses para la entidad inmutable-por-
  convención con `slots=True` (menos memoria, atributos fijos) y defaults
  generados (`uuid4`, reloj).
- importa `UTC`, `datetime` desde datetime para sellar `created_at/updated_at`
  en UTC explícito (evita timestamps locales ambiguos).
- importa `UUID`, `uuid4` desde uuid para identidad única de cada documento.
- importa `EntityValidationError` desde app.domain.exceptions para rechazar
  estados imposibles en `__post_init__`.
- importa `DocumentStatus`, `DocumentType` desde app.domain.value_objects.enums
  para la máquina de estados (RECEIVED→PROCESSING→PROCESSED|FAILED) y el tipo
  (PDF/IMAGE); son StrEnum del propio dominio.

#### app/domain/entities/invoice.py
Raíz del agregado facturas (+ `Supplier`, `InvoiceItem`). Imports:
- dataclasses/datetime/Decimal/uuid por las mismas razones que document.py;
  además `date` para fechas de emisión/vencimiento y `Decimal` para cantidades
  y precios de línea sin error binario de float.
- importa `EntityValidationError` para los invariantes de nivel 2 (número
  requerido, moneda ISO-3 mayúscula, ≥1 línea, total > 0).
- importa `InvoiceValidationStatus` (enums) para PENDING/VALID/INVALID.
- importa `Money` (value_objects.money) para subtotal/tax/total cuantizados.
- importa `ValidationReport` (value_objects.validation) para
  `apply_validation(report)` → fija VALID/INVALID + guarda el dict trazable.

#### app/domain/entities/job.py
Unidad de trabajo asíncrono. Imports:
- dataclasses/datetime(UTC)/uuid igual que las demás entidades.
- importa `EntityValidationError` para blindar transiciones ilegales
  (reiniciar un COMPLETED, completar un FAILED) y `attempts >= 0`.
- importa `JobStatus` para la máquina PENDING→PROCESSING→COMPLETED|FAILED y
  `is_terminal`/`can_be_processed` usados por las guardias de idempotencia.

### Repositories (puertos de persistencia)

#### app/domain/repositories/\_\_init\_\_.py
Agrega los 4 puertos en un solo namespace; es quien detectó que faltaba
`supplier_repository` (bug histórico corregido).

#### app/domain/repositories/document_repository.py
- importa `ABC`, `abstractmethod` desde abc para definir el contrato sin
  implementación (el adaptador SQLAlchemy lo cumple).
- importa `UUID` desde uuid para firmar `get(supplier_id: UUID)` con tipo real.
- importa `Document` desde app.domain.entities para tipar entrada/salida con la
  entidad de dominio (nunca modelos ORM).

#### app/domain/repositories/invoice_repository.py
El más rico. Imports:
- abc/abstractmethod por el mismo patrón de puerto.
- `dataclass` para los objetos de consulta/página: `InvoiceQuery` (search,
  validation_status, date_from/to, limit/offset), `InvoiceSummary` (proyección
  plana para listados) e `InvoiceStats` (contadores dashboard).
- `date` y `Decimal` para filtros temporales y totales agregados exactos.
- `UUID` para claves.
- importa `Invoice` para devolver agregados completos en get/add.

#### app/domain/repositories/job_repository.py
Igual que document_repository pero para `ProcessingJob`; añade
`count_by_status()` (dashboard) y `get_by_document()` no aplica aquí —
sí en invoices. Imports: abc, uuid, entidad ProcessingJob.

#### app/domain/repositories/supplier_repository.py
Puerto creado para deduplicar emisores. Imports:
- importa `uuid` (módulo completo) para `get(supplier_id: uuid.UUID)`;
- abc/abstractmethod por el patrón de puerto;
- importa `Supplier` desde app.domain.entities.invoice porque el proveedor vive
  dentro del agregado factura.

### Services (puertos técnicos + servicio puro)

#### app/domain/services/\_\_init\_\_.py
Namespace de los puertos: storage, extractor, OCR, validador.

#### app/domain/services/document_storage.py
Contrato de blobs. Imports: abc/abstractmethod (puerto), `UUID` (claves) y
`StorageError` desde exceptions porque el contrato define QUE fallos de disco
se traducen a error de dominio (con `retryable` según el caso).

#### app/domain/services/invoice_extractor.py
Contrato del LLM. Imports: abc/abstractmethod y `ExtractedInvoiceData` — el
único tipo que un extractor puede devolver; así ninguna respuesta cruda entra
al dominio sin validar.

#### app/domain/services/ocr_provider.py
Contrato de texto. Imports: abc/abstractmethod, `dataclass` para `OCRResult`
(text, page_count, method — el método queda auditado) y `DocumentType` para
firmar `extract_text(content, document_type)`.

#### app/domain/services/invoice_validator.py
Servicio puro de nivel 3 (sin I/O). Imports:
- `Decimal` para tolerancias contables exactas (0.02).
- importa `Invoice` para operar sobre el agregado ya construido.
- importa `Severity`, `ValidationIssue`, `ValidationReport` para producir hallazgos
  clasificados (ERROR invalida; WARNING solo informa) serializables a JSONB.

### Value Objects

#### app/domain/value_objects/\_\_init\_\_.py
Barrel de Money, FileType, Severity/ValidationIssue/ValidationReport, enums y
ExtractedInvoiceData.

#### app/domain/value_objects/enums.py
Solo importa `StrEnum` (py3.12): los enums serializan su valor textual directo
a JSON/BD ("PENDING", "VALID"…).

#### app/domain/value_objects/money.py
Dinero seguro. Imports:
- importa `dataclass` para el VO congelado (`frozen=True`, ordenable para sort).
- importa `ROUND_HALF_UP`, `Decimal`, `InvalidOperation` desde decimal:
  redondeo bancario a 2 decimales, aritmética exacta, y captura del error de
  parseo para traducirlo a `MoneyError`.

#### app/domain/value_objects/validation.py
Piezas del reporte. Imports: dataclasses (issue/report inmutables o mutables
según rol), `StrEnum` para Severity (ERROR/WARNING/INFO).

#### app/domain/value_objects/file_type.py
Identidad del fichero. Imports:
- importa `unicodedata` para normalizar NFKD y descartar acentos antes de
  sanear nombres ("facturá.pdf" → "factura.pdf").
- importa `dataclass` para el resultado de validación inmutable.
- importa `EmptyFileError`, `InvalidFileError` para fallar con códigos claros.
- importa `DocumentType` para cruzar extensión↔magic bytes↔MIME (PDF vs IMAGE).

#### app/domain/value_objects/extracted_invoice.py
Contrato Pydantic del LLM (nivel 1). Imports:
- importa `re` para la regex de moneda ISO `^[A-Z]{3}$`.
- importa `date` para convertir fechas ISO reales, no strings.
- importa `Decimal` para montos exactos en items/subtotal/tax/total.
- importa `BaseModel`, `ConfigDict`, `EmailStr`, `Field`, `field_validator`,
  `model_validator` desde pydantic: esquema con alias (`date`→issue_date),
  coerción "1.234,56"→Decimal, email válido opcional y validación cruzada
  (due_date ≥ issue_date; items mínimo 1).

---

## Application (orquestación de casos de uso)

### app/application/\_\_init\_\_.py
Reexporta casos de uso y DTO principales (conveniencia de imports).

### app/application/dto/

#### \_\_init\_\_.py · dashboard_stats.py
- importa `dataclass` para `DashboardStats` inmutable (contadores jobs/facturas).
- importa `Decimal` para `total_invoiced`: sumas monetarias nunca en float.

### app/application/services/

#### \_\_init\_\_.py
Expone `TaskDispatcher` y `UnitOfWork` (los dos puertos de aplicación).

#### task_dispatcher.py
- importa `ABC`, `abstractmethod`: puerto "manda este job a la cola". La app no
  sabe si detrás hay Celery o un handler inline de test.
- importa `UUID` para tipar `dispatch_invoice_processing(job_id)`.

#### unit_of_work.py
- importa `ABC`, `abstractmethod` para el contrato commit/rollback/repos.
- importa `TracebackType` desde types para firmar correctamente
  `__exit__(exc_type, exc_value, traceback)` del context manager.
- importa los 4 repositorios de dominio: el UoW AGREGA los repos de una misma
  transacción — esa es toda la idea del patrón.

### app/application/use_cases/

#### \_\_init\_\_.py
Barrel de los seis casos de uso (incluye UploadCommand para routers).

#### upload_invoice.py
Subida + encolado. Imports:
- `logging` para trazas estructuradas con document_id/job_id en `extra`.
- `Callable` desde collections.abc para recibir `uow_factory` inyectada
  (callable que devuelve un UoW fresco por ejecución — thread-safe).
- `dataclass` para `UploadCommand`/`UploadResult` (DTOs inmutables agnósticos
  del transporte HTTP/UI).
- `UUID` para identificadores en el resultado.
- importa `TaskDispatcher` (puerto app) para encolar DESPUÉS de persistir.
- importa `UnitOfWork` para abrir la transacción document+job.
- importa `Document`, `ProcessingJob` (entidades) para crear el par coherente.
- importa `FileTooLargeError` para rechazar tamaños fuera de política.
- importa `DocumentStorage` (puerto dominio) para guardar el blob.
- importa `FileType` para validar extensión+MIME+magic bytes y sanear nombre.

#### process_invoice.py
El pipeline completo (ver manual §3.3). Imports:
- `logging` para trazas por etapa con ctx {job_id, document_id}.
- `time` para medir duración de llamadas LLM (observabilidad ligera).
- `Callable` para la fábrica de UoW (misma razón que upload).
- `UUID` para navegar job_id/document_id/invoice_id.
- importa `UnitOfWork` para transacciones cortas alrededor de _begin/_persist.
- importa `Invoice`, `InvoiceItem`, `Supplier` para construir el agregado
  (items ANTES del constructor: invariantes) y deduplicar supplier.
- importa excepciones de dominio (AppError, JobNotFoundError,
  DocumentNotFoundError, DocumentProcessingError) para la estrategia central
  `_handle_failure`: retryable→deja PROCESSING; permanente→FAILED+documento.
- importa los cuatro puertos técnicos (`DocumentStorage`, `InvoiceExtractor`,
  `OCRProvider`, `InvoiceBusinessValidator`) inyectados por constructor.
- importa `JobStatus` para las guardias de idempotencia/resume.
- importa `ExtractedInvoiceData` como ÚNICO formato aceptado del LLM.
- importa `Money` para parsear montos del DTO hacia VOs cuantizados.

#### get_invoice.py
Lectura de detalle. Imports: Callable (uow_factory), UUID, UnitOfWork,
entidades `Invoice, Supplier` (respuesta emparejada), `InvoiceNotFoundError`.

#### get_job_status.py
Polling. Imports: Callable, UUID, UnitOfWork, `ProcessingJob`,
`JobNotFoundError`.

#### list_invoices.py
Listado paginado. Imports: Callable, UnitOfWork y del repositorio de facturas
`InvoiceListPage, InvoiceQuery` — el caso de uso no conoce SQL, solo la query
object del puerto.

#### dashboard_stats.py
KPIs. Imports: Callable, `DashboardStats` (DTO destino), `UnitOfWork`,
`JobStatus` para contar jobs por estado sin hardcodear strings.

---

## Infrastructure (adaptadores concretos)

### app/infrastructure/\_\_init\_\_.py
Vacío; el contenido relevante está en subpaquetes.

### Database

#### database/\_\_init\_\_.py
Expone Base/TimestampMixin/str_enum y session helpers.

#### database/base.py
Cimientos ORM. Imports:
- importa `enum` para aceptar `StrEnum` en `str_enum()`.
- importa `datetime` para columnas de auditoría tipadas.
- importa `DateTime`, `Enum`, `MetaData`, `func` desde sqlalchemy: tipos de
  columna, Enum no-nativo (VARCHAR portátil) y `func.now()` server-side.
- importa `DeclarativeBase`, `Mapped`, `mapped_column` desde sqlalchemy.orm
  para el estilo 2.0 tipado de modelos. La naming convention determinista hace
  migraciones Alembic estables entre entornos.

#### database/session.py
Fábrica de conexiones. Imports:
- importa `Engine` para tipar el retorno.
- importa `create_engine as sa_create_engine` renombrado para no chocar con el
  wrapper propio del mismo nombre.
- importa `Session`, `sessionmaker` desde orm para fabricar sesiones con
  `expire_on_commit=False` (las entidades de dominio siguen usables tras el
  commit — evita lazy-load sorpresas) y pool_pre_ping/recycle=1800 (supervive
  desconexiones idle de Postgres).
- importa `Settings` para leer DATABASE_URL sin tocar el dominio.

#### database/models/ (document_model, supplier_model, invoice_model, job_model, \_\_init\_\_)
Modelos ORM espejo de las entidades. Imports comunes:
- `uuid` para PKs Uuid por defecto.
- Columnas específicas desde sqlalchemy: `BigInteger` (tamaños), `Index`
  (búsquedas por estado/fecha), `String/Text`, `Uuid`, `ForeignKey`
  (job→document, invoice→document/supplier), `DateTime`, `Integer`.
- `JSONB` desde sqlalchemy.dialects.postgresql SOLO en invoice_model para
  `validation_report` y `raw_extraction` consultables.
- `relationship`/`mapped_column` para el agregado invoice↔invoice_items
  (cascade delete-orphan) y `date`/`Decimal` para fechas/números exactos.
- enums de dominio (`DocumentStatus`, `JobStatus`, `InvoiceValidationStatus`)
  + `Base/TimestampMixin/str_enum` base: la BD guarda el VALOR textual
  ("PENDING"), no el nombre Python.
- `supplier_model` NO importa enums: es tabla catálogo simple (name, tax_id
  UNIQUE).

### Repositories (implementación SQLAlchemy)

#### repositories/\_\_init\_\_.py
Expone `SqlAlchemyUnitOfWork` (la pieza que todos instancian).

#### repositories/mappers.py
Capa anti-corrupción entidad↔modelo. Imports: `date/datetime/Decimal`
(conversiones explícitas), entidades de dominio (destino del mapeo), enums y
`Money` (reconstruir VOs desde NUMERIC), y los 5 modelos ORM (origen). Todo el
ruido de conversión vive aquí; los repos quedan finos.

#### repositories/sqlalchemy_document_repository.py
- `uuid` para búsquedas por id.
- `Session` desde sqlalchemy.orm (2.0: Session ya NO está en la raíz — bug
  corregido durante los tests).
- Entidad `Document` + puerto `DocumentRepository` que implementa.
- `DocumentStatus, DocumentType` para reconstruir enums al hidratar.
- `DocumentModel` y mappers `apply_document/document_to_domain`.

#### repositories/sqlalchemy_supplier_repository.py
- `select` desde sqlalchemy para `find_by_tax_id` (WHERE tax_id = …first()).
- `Session` (orm), entidad `Supplier`, puerto `SupplierRepository`,
  `SupplierModel` y mappers `build_supplier_model/supplier_to_domain`.

#### repositories/sqlalchemy_job_repository.py
- `func, select` para `count_by_status()` (GROUP BY status) y queries por id.
- `Session` (orm), entidad/puerto de jobs, `JobStatus` para filtros tipados,
  `ProcessingJobModel` y mappers `apply_job/job_to_domain`.
- `PersistenceError` para traducir fallos de flush.

#### repositories/sqlalchemy_invoice_repository.py
El más complejo (queries+stats). Imports:
- `case, func, or_, select` desde sqlalchemy: búsqueda fuzzy
  (`OR(number ILIKE, tax_id ILIKE)`), filtro condicional por estado con CASE,
  agregaciones SUM/COUNT para stats y paginación.
- `Decimal` para `InvoiceStats.total_invoiced` exacto.
- `Session` (orm), entidad `Invoice`, puerto completo
  (`InvoiceRepository, InvoiceQuery, InvoiceListPage, InvoiceSummary, InvoiceStats`),
  `InvoiceValidationStatus`, modelos `InvoiceModel, SupplierModel` (JOIN para
  traer el nombre del proveedor en summaries) y varios mappers.

#### repositories/unit_of_work.py
UoW transaccional. Imports:
- `TracebackType` para firmar `__exit__`.
- `IntegrityError, SQLAlchemyError` desde sqlalchemy.exc: Integrity→
  `PersistenceError(retryable=False)` (un duplicado no se arregla reintentando);
  otros→retryable=True.
- `Session, sessionmaker` (orm) para una sesión por unidad de trabajo.
- importa el PUERTO `UnitOfWork` (application) que implementa — la dependencia
  apunta hacia dentro, nunca al revés.
- importa `PersistenceError` y los cuatro repos concretos para componerlos
  sobre la MISMA sesión.

### Storage

#### storage/\_\_init\_\_.py
Fábrica `build_document_storage(settings)`: hoy devuelve Local, mañana S3.
Importa `Settings`, `ConfigurationError` (proveedor desconocido), el puerto y
`LocalDocumentStorage`.

#### storage/local_storage.py
Blob en disco. Imports:
- `logging` para auditar saves/deletes con storage_key y bytes.
- `os` para `os.replace()` — escritura ATÓMICA (temporal→destino) que evita
  blobs corruptos si el proceso muere a mitad.
- `re` para `_SAFE_NAME_RE`: neutraliza caracteres peligrosos del filename.
- `tempfile` para crear el temporal EN EL MISMO directorio (rename atómico
  requiere mismo filesystem).
- `datetime(UTC)` para la clave fechada `YYYY/MM/<id>__<file>` (directorios
  equilibrados, fácil purga por mes).
- `Path` para resolve()/is_relative_to(): defensa anti path-traversal
  (`../../etc/passwd` → StorageError).
- `UUID` para firmar save().
- `StorageError` y el puerto `DocumentStorage` (traducción de OSError→dominio).

### OCR

#### ocr/\_\_init\_\_.py
Fábrica `build_ocr_provider(settings)` (hoy solo "local"); `ConfigurationError`
si el proveedor no existe.

#### ocr/local_ocr.py
"OCR solo cuando hace falta". Imports:
- `io` para envolver bytes de imagen en buffer para PIL.
- `logging` para registrar páginas embebidas vs OCR.
- `pymupdf` para abrir el PDF, leer texto por página y rasterizar escaneadas
  (get_pixmap 200 DPI).
- `pytesseract` para OCR Tesseract (eng+spa) de esas páginas/imágenes.
- `PIL.Image` desde pillow para decodificar PNG/JPEG antes de Tesseract.
- `OCRExtractionError` para traducir CUALQUIER excepción de librería a error
  de dominio (el resto del sistema nunca ve pymupdf/pytesseract).
- Puerto `OCRProvider`+`OCRResult` y `DocumentType` para elegir estrategia PDF
  vs IMAGE.

### LLM

#### llm/\_\_init\_\_.py
Fábrica `build_invoice_extractor(settings)`: "mock"→Mock, "openai"→OpenAI-
compatible, otra cosa→`ConfigurationError`.

#### llm/mock_extractor.py
Extractor offline determinista. Imports:
- `hashlib` para sembrar el PRNG con SHA-256 del texto: mismo documento →
  MISMA factura siempre (demos/e2e reproducibles).
- `random` para variar proveedores/líneas/fechas de forma reproducible.
- `date, timedelta` para issue_date retrocedida y due_date +30 días.
- `ROUND_HALF_UP, Decimal` para matemática SIEMPRE consistente (subtotal=Σ
  líneas netas; IVA 21% cuantizado; total=subtotal+tasa).
- Puerto `InvoiceExtractor` y VOs `ExtractedInvoiceData/(Item, Supplier)` como
  salida válida del contrato.

#### llm/openai_extractor.py
Adaptador real (OpenAI/Azure/OpenRouter/vLLM/Ollama). Imports:
- `json` para parsear la respuesta del chat-completions.
- `logging` sin filtrar secretos (la API key jamás entra al log).
- `re` para limpiar fences ```json``` que algunos modelos añaden.
- `time` para backoff exponencial entre reintentos (cap 8s).
- `httpx` cliente HTTP con timeout duro hacia POST {base_url}/chat/completions.
- `SecretStr, ValidationError` desde pydantic: ocultar key en reprs y detectar
  respuestas que no cumplen el esquema.
- `LLMExtractionError` (contrato: falla SIEMPRE con este error), el puerto y
  `ExtractedInvoiceData`. Recorta textos >100k chars y reintenta solo status
  {408,409,429,5xx}.

### Celery

#### celery_app/\_\_init\_\_.py
SIN re-exports a propósito: tasks importa container y container importa
celery_app.app; re-exportar tasks aquí creaba import circular (bug corregido).

#### celery_app/app.py
Instancia Celery compartida. Imports:
- `Celery` para broker/backend Redis, cola única `invoices`, `task_acks_late=True`
  (redelivery si un worker muere a mitad), prefetch=1 (reparto justo),
  soft/hard time limits desde settings, resultados expiran en 1 h, UTC.
- `get_settings` para TODOS esos valores sin hardcodear.
- `worker_hijack_root_logger=False`: el formato de logs del worker lo decide
  nuestra señal `setup_logging` (signals.py), no Celery.
- Al final importa `celery_app.signals` para registrar los handlers al cargar
  el módulo (side effects) en cada arranque de worker.

#### celery_app/signals.py
Ganchos de señal para trazabilidad total. Imports:
- `logging` para el logger del módulo.
- `signals` de celery: `setup_logging` instala `configure_logging` (mismo
  formatter JSON/texto con origen que la API) y `task_failure` registra ERROR
  con nombre/id/args de la task y traceback (`einfo.type/value/tb`) de toda
  tarea que muera sin manejo.
- `get_settings` + `configure_logging`. NO importa container ni tasks:
  cero riesgo de ciclo.

#### celery_app/dispatcher.py
Adaptador del puerto TaskDispatcher. Imports:
- `logging` para trazar dispatch con task id.
- `Celery` solo para TIPEAR el constructor (no crea instancias).
- `OperationalError` y `TimeoutError as KombuTimeoutError` desde kombu.exceptions
  (renombrado para no chocar con builtins/otros timeouts): broker caído o lento
  se traduce a `ExternalServiceError` retryable.
- puerto `TaskDispatcher` (application) que implementa.

#### celery_app/tasks.py
La tarea del worker. Imports:
- `logging`, `uuid` (parsear job_id string→UUID que viaja por JSON).
- `SoftTimeLimitExceeded` desde celery.exceptions para convertir timeout en
  cierre limpio FAILED (mensaje con segundos configurados).
- `get_settings` para max_retries/backoff/timeout.
- `AppError` para distinguir retryable/permanente en `_handle_failure`.
- `JobStatus` para devolver {"status": COMPLETED|FAILED} legible.
- importa `celery_app` directamente del submódulo .app (NO del paquete: rompe
  el ciclo histórico).
- importa `build_process_invoice_use_case, build_uow` del container: el worker
  usa EXACTAMENTE el mismo cableado que la API. Adjunta celery_task_id al job
  y marca FAILED cuando agota reintentos.

### Raíz de composición y logging

#### container.py
ÚNICO módulo que conoce clases concretas. Imports:
- `lru_cache` para singletons (engine, session factory, storage, dispatcher,
  OCR, LLM) mientras los use cases se construyen FRESCOS por llamada.
- `Engine` (tipado), `Session, sessionmaker` (orm) para la fábrica de sesiones.
- Todos los casos de uso (para sus builders), `Settings/get_settings`,
  `InvoiceQuery` (builder paramétrico usado por el router de listado),
  `DocumentStorage` (tipado del singleton), `celery_app` + `CeleryTaskDispatcher`,
  helpers de sesión, fábricas OCR/LLM/storage y `SqlAlchemyUnitOfWork`.

#### logging_setup.py
Logging estructurado con trazabilidad total de errores. Imports:
- `json` para el formatter JSON sin dependencias extra.
- `logging` obviamente (formatters/handlers/root logger).
- `sys` para stdout (12-factor) y para instalar el excepthook principal.
- `threading` + `ExceptHookArgs` para capturar excepciones no manejadas en
  hilos (Streamlit corre páginas en threads).
- `UTC, datetime` para timestamp ISO con milisegundos.
- `Path` para `_PROJECT_ROOT`: acorta `pathname` a ruta relativa al proyecto.
- `TracebackType` para tipar los hooks.

Piezas clave:
- `JsonFormatter`: TODA línea lleva `module/file/function/line`; si hay
  `exc_info`, añade bloque estructurado `{type, python_module,
  origin{file,function,line}, traceback}` — `origin` es el frame MÁS PROFUNDO
  del traceback (donde se lanzó), vía `_raise_site()`.
- `_install_excepthooks()` (idempotente): `sys.excepthook` +
  `threading.excepthook` → CRITICAL "Uncaught exception" con traceback;
  KeyboardInterrupt se delega al hook por defecto.
- Formato texto: `%(...)s [nombre :: función:línea] mensaje`.
Inyecta `extra` (job_id, document_id…) como campos JSON de primer nivel y
silencia ruido de httpx/httpcore.

---

## Presentation (bordes con humanos/HTTP)

### app/presentation/\_\_init\_\_.py · api/\_\_init\_\_.py
Namespaces; el api expone `create_app`.

### API

#### api/main.py
Fábrica de la aplicación. Imports:
- `APIRouter, FastAPI` para montar `/api/v1` con los tres routers.
- `CORSMiddleware` abierto (MVP: el Streamlit puede vivir en otro host).
- `__version__` desde app para OpenAPI.
- `get_settings` para título/log level.
- `configure_logging`, `register_exception_handlers` y los routers
  (invoices/jobs/dashboard/health). Nota: NO importa container directamente —
  eso lo hacen deps y routers, manteniendo main como pura composición HTTP.

#### api/deps.py
Providers FastAPI que delegan en container. Imports: los cinco use cases
(tipado del retorno) y `container`. Es la ÚNICA costura que sobrescriben los
tests e2e con `dependency_overrides`.

#### api/schemas.py
Contratos Pydantic de request/response. Imports: `date, datetime, Decimal`,
`UUID` y `BaseModel, Field` (validación de query params, ejemplos OpenAPI,
acotaciones limit/offset). Nunca se exponen entidades directas.

#### api/mappers.py
Entidad→schema. Imports: entidades (`Document, Invoice, Supplier,
ProcessingJob`), `InvoiceSummary` (listados) y los schemas de salida. Incluye
placeholder de supplier "(unknown)" si aún no resolvió el JOIN.

#### api/exception_handlers.py
Errores→HTTP uniforme + registro de TODOS los errores. Imports:
- `logging` para registrar: dominio 4xx como WARNING con `error_code` +
  método/path y traceback (`exc_info=True`, así el log muestra el módulo que
  lanzó); 5xx inesperados como CRITICAL; validación de request (422) como
  WARNING con campo/loc fallido. `_request_ctx()` extrae método/path del
  Request para los extras.
- `FastAPI, Request, status`, `RequestValidationError`, `JSONResponse` para
  interceptar y reformatear (incluye el 422 de FastAPI al envoltorio propio).
- el catálogo de excepciones de dominio para la tabla
  AppError→status (404s, 400 file, 413 size, 502 external, 500 persistence).
- Respuesta SIEMPRE sanitizada al cliente; el detalle solo vive en logs.

#### api/routers/invoices.py
Endpoints de facturas. Imports:
- `logging` (subidas), `date` (filtros), `UUID` (path params), `Annotated`
  (el parámetro multipart usa `Annotated[UploadFile, File(...)]` — estilo
  moderno FastAPI, equivalente al default-call pero lint-friendly).
- `APIRouter, Depends, File, Query, UploadFile, status` de FastAPI: multipart,
  parámetros validados y códigos (202 en upload).
- `UploadCommand` (DTO del use case — el router NO recibe entidades).
- `get_settings` para el límite de tamaño en streaming `_read_limited`
  (lee chunks de 1 MB y corta en cuanto excede: nunca bufferiza 10 GB).
- `FileTooLargeError, InvalidFileError` para el mismo contrato que el dominio.
- `container` SOLO para `build_invoice_query` (objeto consulta, no I/O);
  los use cases llegan por `deps` (sobrescribibles en tests).
- deps, mappers y schemas correspondientes.

#### api/routers/jobs.py
Polling de estado. Imports: UUID, APIRouter/Depends, `get_job_status_use_case`,
`job_to_response`, `JobResponse`.

#### api/routers/dashboard.py
KPIs. Imports: APIRouter/Depends, `get_dashboard_stats_use_case`,
`DashboardStatsResponse` (anida jobs{} + invoices{} + total_invoiced float
para JSON).

#### api/routers/health.py
Liveness/readiness. Imports:
- `logging`, `redis as redis_lib` (alias largo para no sombrear settings) —
  ping al broker además del clásico SELECT 1.
- `Response, status` de FastAPI y `text` de sqlalchemy para la consulta
  literal contra la BD vía engine del container.
- `__version__` y `get_settings` para el payload informativo.

### Streamlit UI

#### streamlit/app.py
Shell multi-página. Imports: `os` (API_BASE_URL), `streamlit as st`
(navegación sidebar, config de página), `get_settings` +
`configure_logging`: al arrancar instala el MISMO formatter trazable que
api/worker; sus excepthooks (`sys`+`threading`) capturan cualquier error no
manejado de las páginas en los logs del contenedor con módulo/línea.

#### streamlit/api_client.py
Cliente HTTP tipado hacia la API. Imports:
- `contextlib` (suppress ValueError si el cuerpo de error no es JSON),
  `logging`, `os` (URL base configurable).
- `Any` desde typing para payloads flexibles.
- `httpx` con timeouts y traducción de errores a mensajes UI.
- `streamlit as st` para cache (`st.cache_data`) de listados y toasts.

#### streamlit/pages/upload_invoice.py
Formulario de subida. Imports: `time` (polling con sleep del job tras subir),
`api_client`, `st` (file_uploader + spinner + métricas de estado).

#### streamlit/pages/invoices.py
Listado con filtros. Imports: `api_client`, `st`, `pandas as pd` (DataFrame
bonito en pantalla; dependencia SOLO de presentación, marcada noqa E402).

#### streamlit/pages/invoice_detail.py
Detalle + reporte de validación. Imports: `json` (pretty-print de
validation_report/raw_extraction), `api_client`, `st`, `pd` (tabla de items).

#### streamlit/pages/dashboard.py
Contadores y totales. Imports: `api_client`, `st` (métricas/columnas).

---

# Módulo alembic

## alembic/env.py
Puente Alembic↔metadata. Importa TargetMetadata desde
app.infrastructure.database.base (models se importan en cadena para que
create_all/diff las vean) y lee DATABASE_URL del entorno para autogenerate.

## alembic/versions/0001_initial.py
Migración inicial: crea documents, suppliers, invoices, invoice_items,
processing_jobs con índices y unicidades (tax_id, (supplier_id, number),
document_id UNIQUE en invoices).

---

# Módulo Tests

## tests/conftest.py
Fixtures raíz: `sample_pdf_bytes/sample_png_bytes` (payloads con magic bytes
válidos), `fake_storage/fake_ocr/fake_llm`, y `make_uow` — factory que crea
UN `FakeStore` por test y devuelve UoWs que comparten ese store (semántica de
BD multi-transacción); expone `.store` para aserciones. Imports: pytest, fakes,
FakeStore/FakeUnitOfWork.

## tests/fakes_uow.py
FakeStore (dicts por tabla) + FakeUnitOfWork(store) que implementa el puerto
Unit of Work con repos en memoria. Demuestra que el sistema corre SIN Postgres.

## tests/fakes.py
Adaptadores falsos: `FakeStorage` (dict de blobs, fail_on_get, anti-traversal),
`FakeOCR` (texto fijo, cuenta llamadas), `FakeLLM` (devuelve payload o lanza
excepción configurada), `RecordingDispatcher` (graba enqueues y ejecuta el
pipeline inline si le pasas handler — worker simulado del e2e) y
`make_valid_extraction()` (payload matemáticamente consistente, parametrizable
con overrides como total="5000.00" para probar facturas INVALIDAS).
Imports: typing.Callable, uuid.UUID, puerto TaskDispatcher, excepciones,
puertos de repositorio/servicios y ExtractedInvoiceData.

## tests/unit/
- test_money.py — parseo (comas europeas, None→default, ""→error), cuantización
  HALF_UP, negativos/NaN rechazados, is_close.
- test_file_type.py — magic bytes, MIME mentiroso, sanitize_filename.
- test_extracted_schema.py — nivel 1 del contrato LLM (fechas, moneda, ítems).
- test_invoice_validator.py — nivel 3: matemática, fechas, severidades.
- test_mock_llm.py — determinismo y consistencia aritmética del mock.
- test_local_ocr.py — estrategia embebido-vs-Tesseract (stub de pytesseract),
  multi-página, errores→OCRExtractionError.
- test_upload_use_case.py — feliz camino + rechazos (nada persiste si falla).
- test_process_use_case.py — pipeline completo, dedup supplier, INVALID por
  mates rotas, idempotencia/resume, semántica retryable/permanente.
- test_logging_setup.py — trazabilidad: origen (`module/file/function/line`)
  en cada registro JSON/texto; metadatos de excepción (tipo, módulo Python,
  frame de lanzamiento); excepthooks sys/threading → CRITICAL con traceback;
  KeyboardInterrupt delega al hook por defecto; configure idempotente.
- test_exception_handler_logging.py — TODA respuesta de error deja log:
  dominio 4xx→WARNING con error_code/método/path/exc_info; 5xx→CRITICAL;
  422 de validación→WARNING con el campo fallido.

## tests/integration/test_postgres_repositories.py
Repositorios SQLAlchemy contra PostgreSQL REAL (skipif sin TEST_DATABASE_URL):
roundtrips entidad↔fila, unicidad (supplier,number)→PersistenceError
permanente, get_by_document, queries/stats/counters.

## tests/e2e/test_api_offline.py
La app FastAPI REAL con dependency_overrides+fakes y worker inline:
upload→202→poll COMPLETED→detalle VALID→search→stats; contratos de error
400/413/404 con envelope {"error":{code,message}}.

## tests/e2e/test_live_stack.py
Stack completa desplegada (skipif RUN_LIVE_E2E≠1): health, subida de un PDF
REAL generado con pymupdf, polling del worker Celery hasta COMPLETED, detalle
y dashboard por httpx.
