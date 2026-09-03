# Archivo de Estudio — Invoice Processing System

> Guía de repaso para entender, defender y extender el código. Formato
> pregunta/respuesta + catálogo de patrones con referencias a archivos.
> Complementa al `docs/manual_tecnico.md` (visión de referencia).

---

## 1. Las 10 ideas que sostienen todo el proyecto

1. **Hexagonal / puertos y adaptadores** — el dominio no conoce frameworks; la
   infraestructura implementa interfaces del dominio.
2. **Raíz de composición única** (`infrastructure/container.py`) — solo ese
   módulo sabe qué clases concretas existen.
3. **Casos de uso como orquestadores** — reciben puertos por constructor,
   nunca los construyen.
4. **UnitOfWork transaccional** — cada caso de uso abre/cierra su transacción
   (`uow_factory` inyectada como callable).
5. **Agregados con invariantes** — `__post_init__` en entidades: es imposible
   crear una factura sin líneas o un Money negativo.
6. **Contrato Pydantic único para el LLM** (`ExtractedInvoiceData`) — nada no
   validado cruza hacia el dominio.
7. **Errores de dominio con `retryable`** — una sola bandera decide toda la
   política de reintentos (use case + Celery).
8. **Idempotencia del pipeline** — guardia COMPLETED + unicidad
   `(supplier_id, number)` + `get_by_document()` para resumir tras crashes.
9. **Transacciones cortas** — OCR/LLM fuera de tx: sin locks esperando I/O.
10. **Fakes en memoria para tests** — prueban que los bordes son reales; el
    mismo sistema corre con PostgreSQL+Redis o 100% offline.

---

## 2. Preguntas y respuestas rápidas

### Arquitectura

**P. ¿Por qué hexagonal aquí?**
R. El enunciado exige proveedores intercambiables (OCR local→nube, LLM
mock→OpenAI). Con puertos (`OCRProvider`, `InvoiceExtractor`) cambiar de
proveedor = añadir un adaptador y una línea en la fábrica.

**P. ¿Dónde está la única costura para testear la API sin BD?**
R. `presentation/api/deps.py`. Los tests hacen
`app.dependency_overrides[get_upload_invoice_use_case] = lambda: uc_con_fakes`.

**P. ¿Qué pasa si mañana quieren S3 en vez de disco local?**
R. Nueva implementación de `DocumentStorage` (`domain/services/document_storage.py`)
+ entrada en `storage/__init__.py`. Cero cambios en casos de uso ni dominio.

### Dominio

**P. ¿Qué valida cada nivel de validación?**
R. Nivel 1 — sintaxis/tipos en `ExtractedInvoiceData` (Pydantic). Nivel 2 —
invariantes de entidad en `__post_init__` (≥1 ítem, total>0…). Nivel 3 —
reglas de negocio en `InvoiceBusinessValidator` (aritmética subtotal/IVA/total,
fechas, rangos) → `ValidationReport` trazable.

**P. ¿Convención de importes?**
R. NETA: `item.total = cantidad × precio_unitario`; `invoice.subtotal = Σ
item.total`; `invoice.total = subtotal + tax_amount`. La comparten extractor
mock y validador; el validador marca descuadres ≤0.02 como WARNING de redondeo
y >0.02 como ERROR.

**P. ¿Por qué `Money` y no Decimal suelto?**
R. Cuantización garantizada a 2 decimales ROUND_HALF_UP, no negatividad,
comparación tolerante (`is_close`) y parsing defensivo de formatos europeos
("12,5", "1.234,56").

**P. Máquina de estados de un job.**
R. `PENDING → PROCESSING → COMPLETED | FAILED`. Cada `start()` incrementa
`attempts`; los reintentos permanecen en PROCESSING. No se reinicia COMPLETED
ni se completa FAILED. `is_terminal` guía las guardias de idempotencia.

**P. ¿Cómo se deduplican proveedores?**
R. `find_by_tax_id(tax_id)` antes de insertar; si existe se reutiliza su id.
En BD, `tax_id` UNIQUE como última línea de defensa.

### Pipeline asíncrono

**P. Recorrido completo de una subida.**
R. Upload → validar (tamaño/magic bytes) → blob → INSERT document+job(PENDING)
→ enqueue Redis → worker toma tarea → job PROCESSING → texto (embebido u OCR)
→ LLM → agregado → validador → commit (supplier/invoice/items) → job COMPLETED
enlazando invoice_id.

**P. ¿Y si el worker muere justo después de guardar la factura pero antes de
marcar COMPLETED?**
R. Reintento/redelivery vuelve a `_begin()`: el job no está COMPLETED pero
`get_by_document()` encuentra la factura → se enlaza y completa SIN re-procesar
(test `test_resume_links_existing_invoice_after_lost_completion`).

**P. ¿Qué errores se reintentan y cuáles no?**
R. `exc.retryable=True` (timeouts, red): el use case deja PROCESSING y la task
Celery reintenta con backoff 30·2^n hasta 3 veces; agotados → FAILED.
`retryable=False` (fichero corrupto, duplicado, job/documento inexistente):
el use case persiste FAILED antes de relanzar y la task NO relanza.

**P. ¿Por qué "enqueue después de persistir"?**
R. Si la cola confirma antes que la BD, un fallo de commit deja un mensaje que
procesará un trabajo fantasma (JobNotFoundError siempre). Persistir primero =
ningún mensaje apunta a nada inexistente.

**P. ¿Cómo evita locks largos en BD?**
R. `_begin` y `_persist` son transacciones cortas; OCR y LLM ocurren ENTRE
ambas, sin sesión abierta. Escalar workers no satura Postgres.

### Infraestructura

**P. ¿Estrategia OCR?**
R. "OCR solo cuando hace falta": PDF con capa de texto densa (≥40 chars/página)
se lee con PyMuPDF; página escaneada → rasterizado 200 DPI → Tesseract
(eng+spa); imágenes directas a Tesseract. El resultado informa `method`
(p.ej. "embedded-text+tesseract (ocr_pages=1)").

**P. ¿Cómo funciona el extractor mock?**
R. Semilla = primeros 16 hex del SHA-256 del texto → `random.Random(seed)`.
Mismo documento produce SIEMPRE la misma factura (reproducible), con matemática
consistente por construcción. `LLM_PROVIDER=openai` cambia al adaptador real.

**P. ¿Cómo mapea errores SQLAlchemy a dominio?**
R. `IntegrityError` → `PersistenceError(retryable=False)` (un duplicado nunca
se cura reintentando); otros `SQLAlchemyError` → retryable=True.

### API

**P. ¿Formato de error HTTP?**
R. Uniforme: `{"error": {"code": "...", "message": "..."}}`.
Mapa: not-founds→404 · invalid_file/empty_file→400 · file_too_large→413 ·
business_validation→422 · external_service→502 · persistence/processing→500.
El handler global sanitiza errores inesperados (detalle solo en logs).

**P. ¿Por qué polling y no WebSockets/SSE?**
R. MVP con infra mínima: `GET /jobs/{id}` + `poll_url` en la respuesta de
subida bastan; la UI consulta periódicamente.

### Testing

**P. ¿Fakes vs mocks — por qué fakes?**
R. Un fake es una implementación pequeña pero REAL del puerto (dicts en RAM);
ejercita el código de producción de verdad y no acopla el test a detalles de
llamadas. Solo se stubbean librerías físicas imposibles en CI (pytesseract).

**P. ¿Cómo simulan los fakes una BD multi-transacción?**
R. `FakeStore` contiene los dicts; `FakeUnitOfWork(store)` envuelve SIEMPRE el
mismo store → varias "sesiones" ven los mismos datos, como sesiones reales.

**P. ¿Qué hace el e2e offline cuando "sube" un fichero?**
R. TestClient → app real → override del upload UC (fakes) → dispatcher con
`inline_handler` que ejecuta ProcessInvoiceUseCase síncronamente = worker
simulado; después poll, detalle, listado y dashboard sobre la misma app.

**P. ¿Cómo se ejecutan las suites?**
R. `pytest -m unit` (offline) · `-m e2e` (API+fakes) ·
`TEST_DATABASE_URL=... pytest -m integration` (Postgres real) ·
`RUN_LIVE_E2E=1 pytest tests/e2e/test_live_stack.py` (compose arriba).
Sin variables, integración/vivo se saltan solos.

---

## 3. Catálogo de patrones (dónde mirar)

| Patrón | Archivo |
|---|---|
| Hexagonal (ports & adapters) | `app/domain/services/*`, `app/domain/repositories/*`, `app/infrastructure/**` |
| Composition Root | `app/infrastructure/container.py`, `app/presentation/api/deps.py` |
| Use Case / Application Service | `app/application/use_cases/*.py` |
| Unit of Work | `application/services/unit_of_work.py` + `infrastructure/repositories/unit_of_work.py` |
| Repository | `infrastructure/repositories/sqlalchemy_*.py` |
| Aggregate Root + invariantes | `domain/entities/invoice.py`, `job.py`, `document.py` |
| Value Object | `money.py`, `file_type.py`, `validation.py`, enums |
| DTO | `UploadCommand/Result`, `ExtractedInvoiceData`, schemas FastAPI |
| Anti-Corruption Layer | `infrastructure/repositories/mappers.py` |
| Strategy | `ocr/local_ocr.py` vs futuro Textract; `mock_extractor` vs `openai_extractor` |
| Factory Method | `build_ocr_provider`, `build_invoice_extractor`, `build_document_storage` |
| State Machine implícita | `ProcessingJob.start/complete/fail` |
| Outbox-ish ordering | enqueue tras commit en `upload_invoice.py` |
| Idempotent Consumer | `_begin()` en `process_invoice.py` |
| Dependency Injection | constructores de use cases + `Depends` de FastAPI |
| Observabilidad trazable | `logging_setup.py` (origen en cada registro + excepthooks), `celery_app/signals.py` (`setup_logging`/`task_failure`), handlers que registran todo error HTTP |

---

## 4. Bugs reales encontrados por la suite (y lecciones)

1. **Factura siempre inválida**: `_build_invoice` construía el agregado vacío y
   luego añadía líneas, pero el constructor exige ≥1 ítem →
   `EntityValidationError` en cada pipeline. *Lección*: los invariantes de
   agregado deben satisfacerse ANTES de construirlo (los tests unitarios del
   pipeline lo destaparon al instante).
2. **Import circular** `container ↔ celery_app.__init__`: los re-exports ansiosos
   del paquete Celery importaban tasks→container durante la inicialización.
   Solución: paquete sin re-exports, imports directos de submódulos.
3. **Puerto faltante** `SupplierRepository`: la infraestructura lo implementaba
   pero el ABC nunca existió → ImportError al primer arranque. *Lección*: los
   `__init__` de dominio documentan qué debe existir.
4. **`from sqlalchemy import Session`**: en SQLAlchemy 2.0 Session vive en
   `sqlalchemy.orm`. Cinco repos afectados.
5. **Money.parse("12,5") fallaba y "" devolvía 0**: parse ahora normaliza
   separadores europeos y lanza `MoneyError` en cadenas vacías (None sigue
   usando default). Diferencia sutil None-vs-"" con contrato testado.
6. **Descuadre de centavos silencioso**: una línea declarada 100.01 vs qty×precio
   100.00 no generaba aviso (tolerancia absorbía el diff). Ahora hay WARNING
   explícito `math.item_line_rounding` — auditoría sin castigar redondeos.

---

## 5. Guion de 60 segundos (para explicar el proyecto)

"Sistema de facturas con arquitectura hexagonal en Python 3.12. Subes un PDF o
imagen a una API FastAPI: valida tamaño y magic bytes, guarda el blob, registra
documento+job en Postgres y encola con Celery/Redis —en ese orden, para no crear
trabajos fantasma. Un worker ejecuta el pipeline: extrae texto (PyMuPDF y
Tesseract solo si la página está escaneada), un LLM devuelve datos estructurados
validados con Pydantic —hay un proveedor mock determinista para desarrollo y
OpenAI para producción—, un servicio de dominio puro valida la aritmética de la
factura y todo se persiste transaccionalmente con SQLAlchemy, deduplicando
proveedores por NIF. La política de reintentos se gobierna con un atributo
retryable en las excepciones de dominio; los crashes entre commits se recuperan
por idempotencia. Todo corre en docker-compose con healthchecks y migraciones
Alembic. La prueba más fuerte: la suite usa fakes en memoria que sustituyen
Postgres, Redis, OCR y LLM —unit y e2e offline corren sin ningún servicio—, y
las mismas pruebas contra PostgreSQL real están marcadas como integración."

---

## 6. Ejercicios propuestos (autoevaluación)

1. Añade soporte `.heic` end-to-end (¿qué tocas?: whitelist, magic bytes, MIME,
   OCR image path, tests).
2. Implementa `S3DocumentStorage` y su entrada de fábrica (¿cambia algún use
   case? debería ser "no").
3. Añade endpoint `POST /api/v1/jobs/{id}/requeue` para relanzar FAILED
   manualmente (`can_be_processed` ya lo permite).
4. Cambia la convención de importes a BRUTA (`item.total` con IVA incluido):
   identifica TODOS los puntos que rompen (extractor, validador, tests, mapper).
5. Haz el dashboard incremental con cache Redis de 30 s (¿dónde se inyecta?).
6. Escribe el test que demuestre que dos uploads del MISMO pdf generan UNA sola
   factura si comparten `(supplier_id, number)` (pista: PersistenceError).
