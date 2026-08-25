# Capa Dominio — Detalle clase por clase

Documento complementario al §2 de `desglose_componentes.md`. Explica **cada clase y cada función** del dominio (`app/domain/`): qué modela, qué propiedades tiene y cuál es la función de cada método.

El dominio es el núcleo de la arquitectura hexagonal: no importa FastAPI ni SQLAlchemy (única excepción deliberada: Pydantic en `extracted_invoice.py`, contrato de borde con los extractores). Todo lo demás son dataclasses puras y lógica de negocio pura, 100% testeables sin infraestructura.

---

## Índice

1. [Entidades — `entities/`](#1-entidades--entities)
2. [Objetos de valor — `value_objects/`](#2-objetos-de-valor--value_objects)
3. [Jerarquía de excepciones — `exceptions.py`](#3-jerarquía-de-excepciones--exceptionspy)
4. [Servicios de dominio (puertos) — `services/`](#4-servicios-de-dominio-puertos--services)
5. [Puertos de repositorio — `repositories/`](#5-puertos-de-repositorio--repositories)

---

## 1. Entidades — `entities/`

### `invoice.py` — El agregado factura

#### `class Supplier`
**Modela** a la parte que emite la factura (proveedor), con las siguientes propiedades:

| Propiedad | Tipo | Función |
|---|---|---|
| `name` | `str` | Razón social. Obligatoria y no vacía. |
| `tax_id` | `str` | NIT/identificación fiscal. Es la **clave natural de deduplicación**: todas las facturas de un mismo emisor comparten un único `Supplier`. |
| `address` | `str \| None` | Dirección fiscal si viene impresa. |
| `phone` | `str \| None` | Teléfono de contacto. |
| `email` | `str \| None` | Correo de contacto. |
| `id` | `UUID` | Identificador técnico (auto-generado con `uuid4`). |
| `created_at` / `updated_at` | `datetime` | Marcas de auditoría en UTC. |

Métodos:
- `__post_init__()` — Invariante de construcción: rechaza `name` o `tax_id` vacíos/blanco con `EntityValidationError`. Un proveedor sin identidad fiscal no puede existir.
- `identity` *(property)* — Devuelve `"NOMBRE (NIT)"`: representación legible para logs y errores.

#### `class InvoiceItem`
**Modela** un renglón de la factura, con las siguientes propiedades:

| Propiedad | Tipo | Función |
|---|---|---|
| `description` | `str` | Concepto del renglón ("Servicio de traducción", etc.). |
| `quantity` | `Decimal` | Cantidad facturada. |
| `unit_price` | `Decimal` | Precio unitario. |
| `tax_amount` | `Decimal` | Impuesto específico de la línea (si lo hay). |
| `total` | `Money` | Total del renglón como objeto de valor monetario. |
| `id` | `UUID` | Identificador técnico. |

Métodos:
- `line_net` *(property)* — Calcula `quantity × unit_price` como `Money`. Es informativo: la coherencia aritmética real la verifica `InvoiceBusinessValidator._check_items`.
- `to_dict()` — Serializa el renglón a dict plano (decimales como `str` para no perder precisión en JSON).

#### `class Invoice` *(raíz del agregado)*
**Modela** la factura completa —los datos extraídos más el resultado de su validación—, con las siguientes propiedades:

| Propiedad | Tipo | Función |
|---|---|---|
| `document_id` | `UUID` | Enlace al documento origen (relación 1:1 → idempotencia del pipeline). |
| `supplier_id` | `UUID` | Referencia al proveedor emisor. |
| `number` | `str` | Número de factura. Obligatorio. |
| `issue_date` | `date` | Fecha de emisión. |
| `currency` | `str` | Código ISO-4217 de 3 letras en mayúsculas (`COP`, `USD`). |
| `subtotal` | `Money` | Base sin impuestos. |
| `tax_amount` | `Money` | Impuesto total (IVA, etc.). |
| `total` | `Money` | Total pagado; estrictamente positivo. |
| `due_date` | `date \| None` | Fecha de vencimiento si está impresa. |
| `validation_report` | `dict \| None` | Veredicto serializado de la validación de negocio (nivel 3). |
| `raw_extraction` | `dict \| None` | Salida cruda del extractor (trazabilidad/debug). |
| `items` | `list[InvoiceItem]` | Renglones; mínimo uno. |
| `id`, `created_at`, `updated_at` | — | Identidad técnica y auditoría UTC. |

Invariantes de **nivel 2** (`__post_init__`): número no vacío · moneda ISO de 3 mayúsculas · al menos un ítem · total > 0. Toda violación lanza `EntityValidationError`.

Métodos:
- `add_item(item)` — Añade un renglón al agregado.
- `items_total` *(property)* — Suma de los totales de los ítems vía `Money.__add__` (arranca desde `0` gracias a `__radd__`). Base de la verificación subtotal↔ítems.
- `apply_validation(report)` — Adjunta el dict del `ValidationReport` y refresca `updated_at`: el veredicto de negocio viaja persistido junto a la factura.

---

### `document.py` — El documento subido

#### `class Document`
**Modela** el archivo que entra por upload (PDF o imagen) y su ciclo de vida, con las siguientes propiedades:

| Propiedad | Tipo | Función |
|---|---|---|
| `filename` | `str` | Nombre original saneado. Obligatorio. |
| `content_type` | `str` | MIME declarado por el cliente. |
| `size_bytes` | `int` | Tamaño en bytes; nunca negativo. |
| `storage_path` | `str` | Clave opaca donde vive el blob (disco hoy, S3 mañana). |
| `document_type` | `DocumentType` | Clasificación `PDF` / `IMAGE`; decide la estrategia de extracción. |
| `status` | `DocumentStatus` | Máquina de estados: `RECEIVED → PROCESSING → PROCESSED \| FAILED`. |
| `id`, `created_at`, `updated_at` | — | Identidad técnica y auditoría UTC. |

Métodos:
- `__post_init__()` — Invariantes: tamaño ≥ 0 y nombre obligatorio.
- `is_processed` *(property)* — Atajo booleano: ¿terminó exitosamente?
- `mark_processing()` — Transición `RECEIVED→PROCESSING`, **idempotente**: si ya está en `PROCESSING` solo refresca `updated_at`. Así un reintento/redelivery de Celery re-entra sin romper la máquina de estados.
- `mark_processed()` — Transición estricta `PROCESSING→PROCESSED`.
- `mark_failed()` — Marca `FAILED` desde cualquier estado excepto `PROCESSED` (no se puede "fallar" lo ya procesado).
- `_transition(expected, target)` *(privado)* — Guardián de la máquina: exige el estado actual exacto antes de mover; si no coincide lanza `EntityValidationError` con diagnóstico `{actual} -> {destino}`.
- `_touch()` *(privado)* — Actualiza `updated_at`.

---

### `job.py` — La unidad de trabajo asíncrona

#### `class ProcessingJob`
**Modela** el seguimiento de la ejecución asíncrona de un documento en Celery, con las siguientes propiedades:

| Propiedad | Tipo | Función |
|---|---|---|
| `document_id` | `UUID` | Documento al que acompaña. |
| `id` | `UUID` | Identificador público (el que consulta la UI vía `/api/v1/jobs/{id}`). |
| `status` | `JobStatus` | `PENDING → PROCESSING → COMPLETED \| FAILED`; los reintentos permanecen en `PROCESSING`. |
| `attempts` | `int` | Ejecuciones iniciadas; nunca negativo. |
| `invoice_id` | `UUID \| None` | Factura resultante al completar. |
| `celery_task_id` | `str \| None` | Id de la task Celery, para correlación de logs. |
| `error_message` | `str \| None` | Mensaje de error truncado a 2000 caracteres. |
| `started_at` / `finished_at` | `datetime \| None` | Primera ejecución y cierre. |
| `created_at` / `updated_at` | `datetime` | Auditoría UTC. |

Métodos:
- `__post_init__()` — Rechaza `attempts < 0`.
- `is_terminal` *(property)* — ¿Estado final (`COMPLETED`/`FAILED`)? El polling de la UI se detiene aquí.
- `can_be_processed` *(property)* — Permite procesar todo lo que **no** esté `COMPLETED`: cubre primer intento (`PENDING`), reencolado manual (`FAILED`) y redelivery/reintento (`PROCESSING`).
- `attach_task(celery_task_id)` — Registra el id de task de Celery.
- `start()` — Pasa a `PROCESSING`, incrementa `attempts` y fija `started_at` solo la primera vez (los reintentos conservan la marca original para medir duración total). Prohibido reiniciar un job completado.
- `complete(invoice_id=None)` — Cierra en `COMPLETED` guardando opcionalmente la factura resultante. Un job fallado no puede completarse directamente.
- `fail(message)` — Cierra en `FAILED` truncando el mensaje a 2000 chars (protección contra payloads desbordados en BD).
- `duration_seconds` *(property)* — Segundos entre `started_at` y `finished_at`, o `None`. Alimenta métricas del dashboard.
- `_utcnow()` *(función de módulo)* — `datetime.now(UTC)` centralizado para todas las marcas temporales de la entidad.

---

## 2. Objetos de valor — `value_objects/`

### `money.py` — Dinero seguro

#### `class MoneyError(ValueError)`
Excepción lanzada cuando un importe monetario es inválido: no-Decimal, NaN/infinito, negativo o texto ilegible.

#### `@dataclass(frozen=True, order=True) class Money`
**Modela** una cantidad monetaria **inmutable**, no negativa y cuantizada a 2 decimales. El dominio jamás manipula floats pelados: todo valor monetario pasa por aquí, garantizando redondeo determinista (`ROUND_HALF_UP`) y aritmética exacta con `Decimal`.

Propiedad única: `amount: Decimal`.

Métodos:
- `__post_init__()` — Triple invariante: debe ser `Decimal` (rechaza `bool` explícitamente porque es subclase de `int`), finito y ≥ 0. Al construir **cuantiza a 2 lugares** vía `object.__setattr__` (necesario porque el dataclass es frozen). Gracias a `order=True` dos `Money` se comparan por importe.
- `parse(raw, *, default="0.00")` *(classmethod)* — Entrada defensiva para datos no confiables (JSON del LLM, formularios): acepta `Decimal/int/str/float/None` (`None` → default), normaliza el formato y ante cualquier fallo lanza `MoneyError` con el valor ofensivo.
- `_normalize(text)` *(staticmethod)* — Acepta formatos europeos/mixtos de miles y decimales: `"12,5" → "12.5"`, `"1.234,56" → "1234.56"`, `"1,234.56" → "1234.56"` (el separador decimal es el que aparece más a la derecha); ignora espacios y NBSP; rechaza cadenas vacías o solo-signo.
- `currency_free_amount` *(property)* — El `Decimal` desnudo, para capas que aún no conocen `Money`.
- `add(other)` / `__add__(other)` — Suma exacta devolviendo un nuevo `Money` (inmutabilidad). `__add__` rechaza tipos ajenos con mensaje claro.
- `__radd__(other)` — Habilita `sum(items)` arrancando desde el entero `0` (cualquier otro tipo lanza `MoneyError`).
- `multiply(factor)` — Multiplicación por `Decimal|int` (p. ej. `quantity × unit_price`), re-cuantizada por el constructor.
- `is_close(other, tolerance=Decimal("0.01"))` — Verdadero si la diferencia absoluta queda dentro de `tolerance`: base de todas las comparaciones monetarias tolerantes (validación de totales, redondeos de línea).
- `__str__()` — Importe formateado a 2 decimales (`"47799.77"`), apto para logs y reportes.

---

### `enums.py` — Máquinas de estado y clasificación

#### `class JobStatus(StrEnum)`
**Modela** el ciclo de vida de un job asíncrono con 4 valores string (`PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`). Al ser `StrEnum` se persiste/compara directamente como texto en BD, JSON y logs.
- `is_terminal` *(property)* — `True` para `COMPLETED|FAILED`: define cuándo un job dejó de trabajar.

#### `class DocumentStatus(StrEnum)`
Estados del documento subido: `RECEIVED` (registrado, sin procesar), `PROCESSING`, `PROCESSED`, `FAILED`.

#### `class DocumentType(StrEnum)`
Clasificación gruesa `PDF` / `IMAGE`. Es el dato que decide qué ruta de extracción aplica (texto embebido vs renderizado+OCR).

---

### `file_type.py` — Identidad del archivo subido

Constantes de módulo:
- `ALLOWED_EXTENSIONS` — `frozenset({"pdf", "png", "jpg", "jpeg"})`: lista blanca de extensiones.
- `DECLARED_MIME_TYPES` — Mapa MIME→`DocumentType` para validar el Content-Type declarado.
- `_MAGIC_SIGNATURES` — Firmas binarias: `%PDF-`, `\x89PNG\r\n\x1a\n`, `\xff\xd8\xff` (JPEG).
- `_PDF_SEARCH_WINDOW = 1024` — La spec de PDF permite el header dentro de los primeros 1024 bytes.

#### `@dataclass(frozen=True) class FileType`
**Modela** el resultado de validar la identidad de un archivo, con las siguientes propiedades:

| Propiedad | Tipo | Función |
|---|---|---|
| `document_type` | `DocumentType` | Familia real detectada (`PDF`/`IMAGE`). |
| `extension` | `str` | Extensión normalizada en minúsculas. |
| `signature_format` | `str` | Formato sniffed: `"PDF"`, `"PNG"`, `"JPEG"`. |
| `mime_type` | `str` | MIME efectivo (el declarado si era válido; si no, el deducido). |

Métodos:
- `sanitize_filename(filename)` *(staticmethod)* — Convierte cualquier nombre (rutas maliciosas, acentos, espacios) en un basename seguro y legible: corta rutas `/` y `\`, translitera Unicode→ASCII, sustituye todo lo no seguro por `_` y recorta puntuación residual; devuelve `"document"` si queda vacío.
- `validate(*, content, filename, declared_mime=None)` *(classmethod)* — Aplica el contrato completo de upload en 5 capas: (1) contenido no vacío → `EmptyFileError`; (2) extensión en lista blanca → `InvalidFileError`; (3) MIME declarado permitido; (4) magic bytes identificados como PDF/PNG/JPEG; (5) **coherencia cruzada**: lo que dice la extensión, el MIME y los bytes debe coincidir (un `.pdf` cuyo contenido es JPEG se rechaza con mensaje específico). Devuelve el `FileType` resultante. Lógica pura sobre bytes, sin libmagic → testeable unitariamente.
- `_sniff(content)` *(classmethod privado)* — Lee las firmas: para PDF busca `%PDF-` dentro de la ventana de 1024 bytes; para PNG/JPEG compara el prefijo exacto. Devuelve un `FileType` preliminar o `None`.

Funciones de módulo:
- `_default_mime(sniffed)` / `_default_mime_by_format(fmt)` — MIME canónico por formato (`PDF→application/pdf`, etc.), usado cuando el cliente no declaró uno válido.

---

### `extracted_invoice.py` — Contrato de salida del extractor (validación nivel 1)

Único archivo del dominio que usa Pydantic, a propósito: es la frontera por donde entra la salida cruda e impredecible de un LLM. **Nada sin validar cruza esta barrera** hacia aplicación/dominio.

#### `class ExtractedSupplier(BaseModel)`
**Modela** el bloque proveedor del esquema de extracción, con las siguientes propiedades: `name` (2–255 chars), `tax_id` (6–64 chars), y opcionales `address` (≤500), `phone` (≤50), `email` (`EmailStr` validado). Configuración: ignora campos extra del LLM (`extra="ignore"`) y recorta espacios en strings.

#### `class ExtractedItem(BaseModel)`
**Modela** un renglón extraído, con las siguientes propiedades: `description` (1–1000), `quantity ≥ 0`, `unit_price ≥ 0`, `tax ≥ 0` (default 0) y `total > 0`.
- `_coerce_amount(...)` *(validator, modo before)* — Antes de validar, limpia strings numéricos del LLM: quita espacios y convierte coma decimal a punto (`"1.234,56"`-friendly).

#### `class ExtractedInvoiceData(BaseModel)`
**Modela** la raíz del contrato estructurado, con las siguientes propiedades: `number` (1–100), `issue_date` (acepta alias `"date"` gracias a `populate_by_name`), `due_date` opcional, `currency`, `subtotal ≥ 0`, `tax ≥ 0`, `total > 0`, `supplier` y `items` (mínimo 1).
- `_currency_iso` *(validator)* — Normaliza a mayúsculas y exige patrón `^[A-Z]{3}$`.
- `_coerce_amounts` *(validator before)* — Misma limpieza numérica tolerante que en los ítems.
- `_check_dates` *(model_validator after)* — Regla transversal: `due_date` no puede ser anterior a `issue_date`.
- `items_total` *(property)* — Suma Decimal de los renglones; materia prima para el chequeo aritmético posterior.

> Los tres niveles de validación: **1** sintaxis/tipos/rangos aquí · **2** invariantes de entidad en `__post_init__` · **3** matemática de negocio en `InvoiceBusinessValidator`.

---

### `validation.py` — Reporte de validación

#### `class Severity(StrEnum)`
Gravedad de un hallazgo: `ERROR` (bloquea: factura inválida), `WARNING` (sospechoso pero tolerado), `INFO` (informativo).

#### `@dataclass(frozen=True) class ValidationIssue`
**Modela** un hallazgo individual y trazable, con las siguientes propiedades: `code` (id estable tipo `"math.total_mismatch"`), `severity`, `message` legible y `field` (ruta del campo culpable, p. ej. `items[2].total`).
- `to_dict()` — Serializa para persistirlo dentro de `Invoice.validation_report`.

#### `@dataclass class ValidationReport`
**Modela** el agregado de hallazgos de una pasada de validación sobre una factura.
- `add(issue)` — Acumula un hallazgo.
- `errors` / `warnings` *(properties)* — Hallazgos filtrados por gravedad.
- `is_valid` *(property)* — `True` solo si no hay ningún `ERROR` (los warnings no invalidan).
- `to_dict()` — Forma serializable `{is_valid, issues:[...]}` que viaja al frontend y a BD.

---

## 3. Jerarquía de excepciones — `exceptions.py`

Contrato de reintentos consumido por la capa Celery: `retryable=True` → fallo transitorio (se reintenta); `retryable=False` → fallo permanente (job FAILED, sin reintentos). Cada clase define su `error_code` estable para APIs/logs.

#### `class AppError(Exception)` *(raíz)*
- Propiedades: `default_retryable` (clase), `error_code` (clase) y `code` *(property)*.
- `__init__(message, *, retryable=None)` — Permite sobreescribir la reintentabilidad puntualmente; por defecto usa el valor de clase.

#### Rama dominio (`DomainError`)
- `DomainError` — Base de errores de negocio. Código `domain_error`.
- `EntityValidationError(DomainError)` — Invariante de entidad violado en construcción/mutación (`entity_validation_error`). No reintentable: el dato es malo.
- `BusinessValidationError(DomainError)` — Reglas de negocio incumplidas (`business_validation_failed`). Además del mensaje carga `issues: list[ValidationIssue]` con el detalle campo por campo.

#### Rama aplicación (`ApplicationError`)
- `NotFoundError` y especializaciones `DocumentNotFoundError`, `JobNotFoundError`, `InvoiceNotFoundError` — Consultas sin resultado (`not_found`, `*_not_found`) → mapean a HTTP 404.
- `InvalidFileError` — Upload rechazado por extensión/MIME/firma (`invalid_file`).
  - `FileTooLargeError` (`file_too_large`) · `EmptyFileError` (`empty_file`).

#### Rama pipeline
- `DocumentProcessingError` — Fallo genérico del pipeline, permanente por defecto (`document_processing_failed`).
- `TransientPipelineError(DocumentProcessingError)` — Mismo tipo pero `default_retryable=True`: un tropiezo recuperable merece reintento Celery (`transient_processing_error`).

#### Rama servicios externos (`ExternalServiceError`, transitorios por naturaleza)
- `ExternalServiceError` — Base reintentable (`external_service_error`).
- `OCRExtractionError` (`ocr_extraction_failed`) — El OCR falló irrecuperablemente.
- `LLMExtractionError` (`llm_extraction_failed`) — Transporte, timeout, JSON inválido o esquema no conforme tras los intentos internos.
- `PartialExtractionError(LLMExtractionError)` — **La excepción clave de la extracción híbrida**: las reglas encontraron *algunos* campos pero no todos. Carga `partial_data` (subset ya serializable: fechas ISO, supplier/items como dicts) y `missing_fields`. Es deliberadamente NO reintentable: reintentar reglas daría el mismo resultado; lo que sigue es escalar al fallback. El trío monetario subtotal/tax/total viaja como bloque coherente cuando se encontró completo.
- `StorageError` (`storage_error`) — Escritura/lectura/eliminación de blobs fallida.

#### Rama infraestructura
- `PersistenceError` — BD caída, lock, pool agotado… `default_retryable=True` (`persistence_error`).
- `ConfigurationError` — Misconfiguración detectada al construir adaptadores en la raíz de composición (`configuration_error`): falla rápido en arranque.

---

## 4. Servicios de dominio (puertos) — `services/`

### `ocr_provider.py` — Puerto de OCR

#### `@dataclass(frozen=True) class OCRResult`
**Modela** el resultado de una pasada de extracción de texto, con las siguientes propiedades: `text` (texto plano con saltos de línea), `page_count` y `method` (etiqueta de trazabilidad: `"embedded-text"`, `"tesseract"`, `"embedded+tesseract"`…).
- `is_empty` *(property)* — ¿Salió vacío? Dispara decisiones de escalada.

#### `class OCRProvider(ABC)`
**Puerto driven** que abstrae *obtener texto de un documento*. Las implementaciones deciden cuándo corre OCR real (solo escaneos/imágenes) y qué motor lo hace (PaddleOCR-VL/paddle/tesseract hoy; Textract/Azure mañana). El resto del sistema solo depende de esta interfaz.
- `extract_text(content, document_type) -> OCRResult` — Contrato único; lanza `OCRExtractionError` ante fallo irrecuperable.

### `invoice_extractor.py` — Puerto LLM

#### `class InvoiceExtractor(ABC)`
**Puerto driven** que convierte texto crudo en datos estructurados *validados*. Contrato estricto:
- Debe devolver un `ExtractedInvoiceData` (Pydantic-validado) o lanzar `LLMExtractionError`; jamás salida cruda sin validar.
- `extract(document_text, images=None)` — `images` son renders PNG/JPG de páginas para adaptadores de visión (Gemini/VL); los adaptadores solo-texto deben ignorarla.
- Timeouts, reintentos y elección de proveedor son asuntos del adaptador, no del contrato.

### `document_storage.py` — Puerto de almacenamiento blob

#### `class DocumentStorage(ABC)`
**Puerto driven** para guardar/recuperar documentos subidos. Las claves son strings opacos (segmentos locales hoy, object keys S3 mañana); las implementaciones deben ser seguras contra path traversal y uso concurrente.
- `save(document_id, filename, content) -> str` — Persiste y devuelve la clave escrita; `StorageError` si falla.
- `get(storage_key) -> bytes` — Lee de vuelta; `StorageError` si la clave es desconocida/ilegible.
- `delete(storage_key)` — Eliminación best-effort: claves ausentes no levantan error.
- `_ensure_not_traversal(key)` *(staticmethod protegido)* — Rechaza claves que contengan segmento `".."` (defensa base anti path-traversal reutilizable por implementadores).

### `invoice_validator.py` — Validación de negocio (nivel 3)

Constantes: `_DEFAULT_TOLERANCE = Decimal("5")` (absorbe redondeos de $1–5 entre subtotal+IVA vs total), `_MAX_LINE_TOLERANCE = Decimal("0.02")` (por línea), `_MAX_FUTURE_DAYS = 1` (tolera skew de reloj).

#### `class InvoiceBusinessValidator`
Validador **sin estado** que opera puramente sobre entidades de dominio (sin Pydantic ni infraestructura) y produce un `ValidationReport` trazable.
- `__init__(tolerance=_DEFAULT_TOLERANCE)` / `tolerance` *(property)* — Régimen de tolerancia configurable por país/proveedor.
- `validate(invoice) -> ValidationReport` — Orquesta las cinco verificaciones en orden:

| Verificación | Qué revisa | Hallazgos típicos |
|---|---|---|
| `_check_required_fields` | número no vacío, supplier presente | `required.number` (ERROR) |
| `_check_dates` | emisión ≤ hoy+1 día (INFO si futuro cercano, WARNING si >1 año); vencimiento ≥ emisión | `date.issue_future` (INFO/WARNING), `date.due_before_issue` (ERROR) |
| `_check_numeric_ranges` | subtotal/tax ≥ 0, total > 0, cantidades positivas | `range.subtotal_negative`, `range.item_quantity` (ERROR) |
| `_check_math` | `subtotal + tax ≈ total` dentro de tolerancia | `math.total_mismatch` (ERROR, con la diferencia exacta en el mensaje) |
| `_check_items` | hay ítems; Σ líneas ≈ subtotal (WARNING si gap ≤ 1¢×n ítems, ERROR si mayor); cada línea `cantidad × precio ≈ total` (ERROR si difiere >2¢; WARNING de redondeo si difiere menos) | `items.empty`, `math.items_subtotal_mismatch`, `math.item_line_mismatch`, `math.item_line_rounding` |

El veredicto no decide nada por sí mismo: el use-case persiste el reporte vía `invoice.apply_validation()` y el pipeline marca la factura válida/inválida según `is_valid`.

---

## 5. Puertos de repositorio — `repositories/`

Interfaces driven implementadas por SQLAlchemy en infraestructura. Ninguna expone detalles de BD.

### `invoice_repository.py`

#### `@dataclass(frozen=True) class InvoiceQuery`
Parámetros de listado/filtrado transport-agnostic: `search` (coincide número o nombre/NIT de proveedor), `date_from`/`date_to`, `limit` (default 20) y `offset`.
- `__post_init__()` — Valida paginación sana: `limit ∈ [1,100]`, `offset ≥ 0`, `date_from ≤ date_to`.

#### `@dataclass(frozen=True) class InvoiceSummary`
Read model plano para listados: id/document_id, number, issue/due date, currency, subtotal/tax/total como `Decimal` y datos del proveedor (`supplier_name`, `supplier_tax_id`). Evita cargar agregados completos al listar.

#### `@dataclass(frozen=True) class InvoiceListPage`
Página resultante: `items: list[InvoiceSummary]` + `total_count` (para paginar el frontend).

#### `@dataclass(frozen=True) class InvoiceStats`
Agregados de dashboard: `total_invoices` y `total_invoiced` (suma monetaria global).

#### `class InvoiceRepository(ABC)` *(puerto del agregado factura)*
- `add(invoice)` — Persiste factura junto con sus renglones.
- `get(invoice_id) -> Invoice \| None` — Trae la factura con ítems cargados eager.
- `get_by_document(document_id) -> Invoice \| None` — Guardia de idempotencia/reanudación: una sola factura por documento procesado (si un job re-procesa, aquí se detecta).
- `query(criteria: InvoiceQuery) -> InvoiceListPage` — Listado filtrado y paginado (más nuevos primero).
- `stats() -> InvoiceStats` — Agregados para el dashboard.

### `supplier_repository.py`
#### `class SupplierRepository(ABC)` *(puerto proveedores)*
- `add(supplier) -> Supplier` — Registra y devuelve el mismo agregado.
- `get(supplier_id) -> Supplier \| None` — Búsqueda por identificador.
- `find_by_tax_id(tax_id) -> Supplier \| None` — La operación estrella: deduplicación de emisores entre facturas (si existe por NIT, se reutiliza en vez de crear duplicado).

### `document_repository.py`
#### `class DocumentRepository(ABC)` *(puerto documentos)*
- `add(document)` — Registra un documento nuevo.
- `get(document_id) -> Document \| None` — Por id.
- `update(document)` — Persiste cambios de estado del agregado existente (transiciones de la máquina de estados de `Document`).

### `job_repository.py`
#### `class JobRepository(ABC)` *(puerto jobs)*
- `add(job)` — Registra un job nuevo (estado PENDING).
- `get(job_id) -> ProcessingJob \| None` — Lo que consulta el endpoint `/jobs/{id}`.
- `update(job)` — Persiste transiciones (`start/complete/fail/attach_task`).
- `count_by_status() -> dict[str, int]` — Contadores por estado (`{"PENDING": 3, ...}`) para salud operacional y dashboard.
