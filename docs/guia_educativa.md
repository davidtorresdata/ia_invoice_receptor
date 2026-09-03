# Guía Educativa: Cómo funciona el Proyecto de Procesamiento de Facturas

> Escrito para alguien que sabe POO y Python pero nunca construyó un servicio como este.
> Vamos de lo simple a lo complejo: primero las ideas, luego el código, luego cómo configurarlo.

---

## Parte 0 — Antes de empezar: ¿qué estamos construyendo?

Imaginá un restaurante que recibe facturas en PDF y necesita guardarlas en una base de datos junto con el proveedor, el número, los montos y los impuestos. Eso es todo: **leer un documento → sacarle los datos → guardarlos de forma ordenada → poder consultarlos**.

Suena simple, pero esconde desafíos:

1. Los PDFs vienen en **formatos distintos** (unos tienen texto digital, otros son escaneos, otros usan layout distinto).
2. Extraer datos de un PDF es **lento** (un segundo o más) — nadie quiere esperar a que termine mientras el botón "subir" carga.
3. Necesitamos que sea **confiable y auditable**: montos que no cuadren deben marcarse, y debe quedar rastro de qué se hizo.

El proyecto resuelve esto con una arquitectura que separa responsabilidades. Este documento te va a dar el mapa completo.

---

## Parte 1 — La imagen mental: una fábrica con departamentos

Pensá el sistema como una **fábrica** con departamentos que no se hablan directamente:

```
┌──────────────────────────────────────────────────────────────────┐
│                        ENTRADA (FastAPI)                         │
│        Recibe el PDF del cliente, le dice "ya lo recibí"         │
└───────────────────────────────┬──────────────────────────────────┘
                                │ (no procesa nada, solo agenda)
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│                 GESTIÓN DE TRABAJO (Celery + Redis)              │
│        Un "administrador de tareas" coordina el procesamiento    │
└───────────────────────────────┬──────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│                 TRABAJADORES (Celery workers)                    │
│   Hacen el trabajo pesado: leer el PDF, extraer datos, validar   │
└───────────────────────────────┬──────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│                   BASE DE DATOS (PostgreSQL)                     │
│         Guarda documentos, proveedores, facturas, jobs           │
└──────────────────────────────────────────────────────────────────┘
                                ▲
                                │
┌──────────────────────────────────────────────────────────────────┐
│               CONSULTA (Streamlit + FastAPI)                     │
│        El usuario mira las facturas en un panel web               │
└──────────────────────────────────────────────────────────────────┘
```

La idea clave: **el servidor web solo recibe la orden y responde "recibido"**. El trabajo pesado ocurre después, en segundo plano. Si el proceso es lento, el usuario no se queda esperando con la página congelada.

---

## Parte 2 — Los bloques que montan el sistema (Docker)

Antes de hablar de código, necesitás saber que todo corre "en cajas" aisladas llamadas **contenedores** (Docker/Podman). Cada caja tiene UNA responsabilidad:

| Contenedor | ¿Qué hace? | Analogía |
|---|---|---|
| `invoice-processor_api_1` | Servidor web (FastAPI). Recibe peticiones HTTP. | La puerta de entrada / recepcionista |
| `invoice-processor_worker_1` | Trabajadores (Celery). Ejecutan el procesamiento. | Los operarios de la fábrica |
| `invoice-processor_postgres_1` | Base de datos (PostgreSQL) | El almacén/archivero |
| `invoice-processor_redis_1` | Cola de mensajes (Redis) | El tablón de anuncios donde se dejan las tareas |
| `invoice-processor_streamlit_1` | Panel web (Streamlit) | La sala de consulta / vitrina |

Juntos forman un **compose** (un grupo de contenedores definido en `docker-compose.yml`). Un comando los levanta a todos.

---

## Parte 3 — Cómo fluye una factura (el recorrido completo)

Seguimos la vida de un PDF desde que llega hasta que se guarda:

**Paso 1 — Subida (API)**
El usuario sube `factura.pdf`. La API (FastAPI) lo valida (¿es realmente un PDF? ¿no es demasiado grande? ¿la extensión coincide con el contenido?). Crea un registro `Document`, crea un `Job` (tarea) en estado `PENDING`, y llama al despachador para agendarlo.

**Paso 2 — Cola (Redis + Celery)**
El despachador deja un "mensaje" en la cola: "procesá el job X". La API responde al cliente: "recibido, task_id: ...". **El usuario ya no espera.**

**Paso 3 — Procesamiento (worker)**
Un worker toma el mensaje de la cola y ejecuta el pipeline:
  1. Marca el job como `PROCESSING` y el documento igual.
  2. Lee el archivo del storage.
  3. **Extrae texto**: si el PDF es digital, saca el texto directamente (PyMuPDF); si es escaneo/imagen, lo pasa por OCR.
  4. **Extrae datos** (el corazón): convierte ese texto en campos `{number, date, subtotal, total, supplier, items...}`. Esto lo hace un "extractor" — detalles más abajo.
  5. **Valida** matemática y reglas de negocio (¿subtotal + IVA = total? ¿fechas coherentes?).
  6. **Persiste**: guarda (o reutiliza) el proveedor, la factura y sus ítems en PostgreSQL, y marca todo como `COMPLETED`.

**Paso 4 — Consulta (API + Streamlit)**
El usuario, en el panel Streamlit, hace *polling*: "¿ya terminó el job X?" cada 2 segundos. Cuando ve `COMPLETED`, muestra la factura con todos sus datos y el reporte de validación.

---

## Parte 4 — El concepto estrella: ARQUITECTURA HEXAGONAL

Este es el diseño más importante del proyecto y probablemente el que te va a costar más entender. Dedicá tiempo aquí.

### 4.1 El problema que resuelve

En una app simple juntás todo: conectar a la BD, lógica de negocio y manejo de peticiones en el mismo lugar. Eso funciona para apps chicas. Pero acá hay **múltiples piezas móviles** (BD, Redis, OCR, un modelo de IA, storage local) y querés que cada pieza sea **reemplazable** y **testeable por separado**.

La arquitectura hexagonal (también llamada *ports & adapters*) define **capas** con reglas de dependencia estrictas:

```
        ┌─────────────────────────────────────────────┐
        │       1. Presentación (FastAPI/Streamlit)     │  ← "afuera"
        └────────────────────┬─────────────────────────┘
                             │  usa
                             ▼
        ┌─────────────────────────────────────────────┐
        │       2. Aplicación (casos de uso)           │
        └────────────────────┬─────────────────────────┘
                             │  usa
                             ▼
        ┌─────────────────────────────────────────────┐
        │       3. Dominio (lógica de negocio pura)    │  ← "adentro" (el núcleo)
        └─────────────────────────────────────────────┘
```

La **regla de oro**: el dominio (núcleo) NO sabe nada de la web, la base de datos ni los servicios externos. Solo conoce su propia lógica pura. Todo lo demás *depende de él*, nunca al revés.

### 4.2 Qué es un "puerto" (Port) y un "adaptador" (Adapter)

Este es EL concepto que pediste explicar. Vamos lento.

Un **puerto** es una **interfaz** (un contrato abstracto, en Python un `ABC` — clase abstracta). Dice *qué* se puede hacer, sin decir *cómo*.

Un **adaptador** es una **implementación concreta** de esa interfaz. Dice *cómo* se hace realmente.

Ejemplo del mundo real: **un tomacorriente**.
- El **puerto** es el enchufe: el estándar "esto conecta a 220V". Define el contrato.
- Los **adaptadores** son los cables/adaptadores que transforman esa energía para distintos aparatos.

En el código:

```python
# app/domain/services/ocr_provider.py  → el PUERTO
from abc import ABC, abstractmethod

class OCRProvider(ABC):                       # ← Puerto (contrato)
    @abstractmethod
    def extract_text(self, content, document_type):
        """Extrae texto de un documento. NO dice cómo."""
        ...

# app/infrastructure/ocr/local_ocr.py  → un ADAPTADOR
class LocalOCRProvider(OCRProvider):          # ← Adaptador (implementación real)
    def extract_text(self, content, document_type):
        # ... implementación real con Tesseract/PyMuPDF ...
        return OCRResult(text=..., page_count=..., method=...)
```

**¿Por qué separar?** Porque podés cambiar el "cómo" sin tocar nada del "resto del sistema". Hoy el OCR usa Tesseract en local; mañana migrás a Amazon Textract y solo escribís un nuevo adaptador que implemente el MISMO puerto `OCRProvider`. El dominio ni se entera.

Los puertos del proyecto:

| Puerto (interfaz) | ¿Qué promete? | Adaptador actual |
|---|---|---|
| `OCRProvider` | extraer texto de un doc | `LocalOCRProvider` |
| `InvoiceExtractor` | texto → datos validados | `RulesInvoiceExtractor`, `OpenAICompatibleInvoiceExtractor`, `HybridInvoiceExtractor`, ... |
| `DocumentStorage` | guardar/leer el PDF en disco | `LocalDocumentStorage` |
| `UnitOfWork` | abrir una transacción de BD | `SqlAlchemyUnitOfWork` |
| `TaskDispatcher` | agendar trabajo asíncrono | `CeleryTaskDispatcher` |
| `DocumentRepository`, `SupplierRepository`, `InvoiceRepository`, `JobRepository` | persistir entidades | `SqlAlchemy*Repository` |

### 4.3 Las capas en detalle

**Capa 3 — DOMINIO (el núcleo)**
Solo lógica pura de negocio. Aquí viven:
- **Entidades** (`Invoice`, `Supplier`, `Document`, `ProcessingJob`): objetos con identidad y reglas internas.
- **Objetos de valor** (`Money`, `FileType`, validaciones): objetos inmutables que representan un valor.
- **Puertos** (interfaces que el dominio necesita): `OCRProvider`, `InvoiceExtractor`, etc.

Ejemplo de regla de negocio del dominio: un `Invoice` **no puede existir sin al menos un ítem**, el total debe ser positivo, y la moneda debe ser de 3 letras. Si intentás construir uno inválido, el propio objeto lo rechaza.

```python
# app/domain/entities/invoice.py
class Invoice:
    def __post_init__(self):
        if not self.number.strip():
            raise EntityValidationError("Invoice number is required")
        if not self.items:
            raise EntityValidationError("Invoice requires at least one line item")
        if self.total.amount <= Decimal("0"):
            raise EntityValidationError("Invoice total must be greater than zero")
```

El dominio no importa ni `fastapi`, ni `sqlalchemy`, ni `requests`. Cero dependencias externas. Eso hace que **testearlo sea trivial** y que **sea imposible que la BD o la web corrompan la lógica**.

**Capa 2 — APLICACIÓN (casos de uso)**
Orquesta las operaciones usando los puertos del dominio. Un "caso de uso" (use case) es un paso de la vida real del negocio: "subir una factura", "procesar una factura", "consultar una factura". Estos NO tienen lógica de negocio; coordinan.

```python
# app/application/use_cases/upload_invoice.py
class UploadInvoiceUseCase:
    def __init__(self, uow_factory, storage, dispatcher, *, max_file_size_bytes):
        ...
    def execute(self, command: UploadCommand) -> UploadResult:
        file_type = self._validate(command)          # usa puerto/validator
        document = Document(...)
        storage_key = self._store(...)               # usa puerto Storage
        job = ProcessingJob(document_id=document.id)
        with self._uow_factory() as uow:             # usa puerto UoW
            uow.documents.add(document)
            uow.jobs.add(job)
            uow.commit()
        self._dispatcher.dispatch_invoice_processing(job.id)  # usa puerto Dispatcher
        return UploadResult(...)
```

**Capa 1 — PRESENTACIÓN (web)**
FastAPI recibe el HTTP, `deps.py` provee los casos de uso (inyección de dependencias), los routers llaman a `use_case.execute(...)`, y `mappers.py` convierte entidades → JSON.

### 4.4 ¿Cómo se conectan todas las piezas? La "raíz de composición"

Alguien tiene que decidir QUÉ adaptador concreto usa cada puerto cuando arranca el programa. Ese lugar se llama **raíz de composición** (composition root). Acá está en `app/infrastructure/container.py`.

```python
# app/infrastructure/container.py (simplificado)
def build_process_invoice_use_case():
    return ProcessInvoiceUseCase(
        uow_factory=build_uow,                          # SQLAlchemy
        storage=get_document_storage(),                 # LocalDocumentStorage
        ocr_provider=_get_ocr_provider(),               # LocalOCRProvider
        extractor=_get_invoice_extractor(),             # decide por LLM_EXECUTION
        validator=_get_invoice_validator(),
    )
```

Si mañana cambiás el store a S3, solo tocás la raíz de composición. Nada más.

---

## Parte 5 — Celery: la gestión de trabajo asíncrono

Ya viste que el servidor web no procesa; agenda. El que procesa es Celery. Veamos el concepto.

### 5.1 El problema

El OCR y la IA pueden tardar de 1 a ~5 minutos. Si FastAPI hiciera eso dentro de la petición HTTP, el navegador se congelaría 5 minutos y, peor, un solo usuario bloquearía el servidor. Insostenible.

### 5.2 La solución: una cola + trabajadores

Pensá en un **pedido de restaurante**:

- **Redis** es la **comanda** (el tablero donde el mozo deja el pedido anotado).
- **Celery workers** son los **cocineros**: cada uno toma pedidos de la comanda y los va preparando.
- El **mensaje** en la cola es el "pedido": una tarea con sus argumentos.

Flow en código:

```python
# app/infrastructure/celery_app/app.py — configura la comanda
celery_app = Celery("invoice_processor", broker=settings.redis_url, backend=settings.redis_url)
```

- `broker`: donde se dejan los mensajes (Redis).
- `backend`: donde se guardan los resultados/estados de cada tarea.

```python
# app/infrastructure/celery_app/tasks.py — define el "pedido" que cocinan
@celery_app.task(name="process_invoice", ...)
def process_invoice_task(self, job_id: str):
    result = use_case.execute(uuid.UUID(job_id))
    return {"status": "COMPLETED"}
```

### 5.3 Cómo se conecta todo

```python
# app/infrastructure/celery_app/dispatcher.py — el puente API→cola
class CeleryTaskDispatcher(TaskDispatcher):
    def dispatch_invoice_processing(self, job_id):
        self._app.send_task("process_invoice", args=[str(job_id)], queue="invoices")
```

La API llama a `dispatcher.dispatch_invoice_processing(job.id)`. Eso pone el mensaje en la cola de Redis. Un worker que está escuchando (`queue="invoices"`) lo recibe y ejecuta la task.

### 5.4 Reintentos y "el job como entidad"

Un paso MUY importante de este proyecto: **el estado del trabajo se guarda en la base de datos**, no solo en Celery. Cada llamada tiene su propio viaje:

```
PENDING ──► PROCESSING ──► COMPLETED
              │
              └──► FAILED (si algo sale mal)
```

`ProcessingJob` (en el dominio) modela ese viaje: cuenta `attempts` (intentos), guarda `error_message`, `started_at`, `finished_at`.

Si una tarea falla por algo **transitorio** (la BD se cayó, timeout de red — errores con `retryable=True`), Celery la **reintenta** con backoff exponencial (espera 30s, luego 60s, luego 120s) mientras queden intentos. Si falla por algo **permanente** (data inválida), se marca `FAILED` y no se re-rodea (reintentar no serviría).

Esta distinción vive en la **jerarquía de excepciones** del dominio (`exceptions.py`), cada error sabe si es `retryable` o no.

---

## Parte 6 — Los SERVICIOS (aclaremos el término)

"Servicio" en este código se usa con dos sentidos. No te confundas:

### Sentido A: "Puertos de servicio" (interfaces del dominio)

En `app/domain/services/` hay interfaces abstractas (`OCRProvider`, `InvoiceExtractor`, `DocumentStorage`) que el dominio necesita. Son abstracciones, no código que corre.

### Sentido B: "Servicios de aplicación" (en `app/application/services/`)

`UnitOfWork` y `TaskDispatcher`: también puertos, pero del lado de aplicación (orquestación).

### Sentido C: contenedores de Docker ("servicios")

En el `docker-compose.yml` cada contenedor se llama "service". `api`, `worker`, `postgres`... son "servicios" en el sentido de Docker.

La idea general que une los tres sentidos: **un servicio es una unidad con una responsabilidad y una interfaz clara**. Se comunica con los demás por contratos, no por detalles internos.

---

## Parte 7 — El corazón: la EXTRACCIÓN HÍBRIDA (reglas + IA + OCR)

Esta es probablemente la parte más interesante. Tenemos tres formas de sacar datos de un documento, combinadas inteligentemente.

### 7.1 Los tres extractores

| Extractor | ¿Qué es? | Cuándo se usa |
|---|---|---|
| `RulesInvoiceExtractor` | **Regex / reglas**, determinista, sin IA | Siempre primero (es gratis, rápido y exacto) |
| `OpenAICompatibleInvoiceExtractor` | **Modelo de visión** remoto (Gemini) | Escalada en modo `api` |
| `PaddleOCRVLEngine` | **OCR local** (PaddleOCR-VL) | Escalada en modo `local` |
| `HybridInvoiceExtractor` | Orquestador que combina reglas + fallback | El modo por defecto |

### 7.2 Cómo decide (el flujo híbrido)

```
Texto del documento
      │
      ▼
Reglas (regex) ── éxito ──► devuelve datos (rápido, sin IA)
      │
      └── faltan campos (PartialExtractionError)
                    │
                    ▼
         Escala al fallback según LLM_EXECUTION:
            api   → Gemini (visión remota)
            local → PaddleOCR-VL + reglas de nuevo
                    │
                    ▼
         FUSIÓN: lo que hallaron las reglas GANA;
         el fallback rellena los huecos.
```

**¿Por qué reglas primero?** Porque son deterministas (misma entrada → misma salida), gratis, y perfectos para facturas electrónicas digitales bien formadas. Solo cuando no alcanzan se gasta el recurso caro (IA).

**La fusión** es delicada: si las reglas hallaron `total=162000` y la IA dice otra cosa, gana `162000`. El trío subtotal/tax/total viaja como **bloque atómico** para que la aritmética no se rompa a medias.

### 7.3 Cómo fan el texto las reglas

Antes de aplicar regex, el texto se **normaliza**: se agregan a MAYÚSCULAS y se quitan acentos (`Electrónica → ELECTRONICA`). Así los patrones regex son simples y no hay que cubrir todas las variantes de tildes o mayúsculas. Esto vive en `text_normalizer.py`.

---

## Parte 8 — Cómo configurar el `.env` (tus datos de funcionamiento)

Ahora el caso práctico. Todo lo que es configuración vive en el archivo `.env` en la raíz. Te explico variable por variable.

### 8.1 La variable MÁS importante: `LLM_EXECUTION`

Este es **el interruptor** que decidís tú (local vs API). Todo lo demás es secundario.

```
LLM_EXECUTION=api    → usar IA en la nube (Gemini)
LLM_EXECUTION=local  → usar IA local (PaddleOCR-VL), sin internet
```

**Regla mental**: `LLM_EXECUTION` decide de dónde sale la "inteligencia" cuando las reglas se quedan cortas.

### 8.2 El `.env` completo explicado (Modo API)

```env
# ============================================================
#  APLICACIÓN
# ============================================================
APP_NAME=Invoice Processing System     # Nombre que se muestra en la UI y API
ENVIRONMENT=development                # development|production. Cambia qué .env se carga
LOG_LEVEL=INFO                         # DEBUG|INFO|WARNING|ERROR. DEBUG = logs detallados
LOG_FORMAT=json                        # json|text. json es más fácil de parsear
CORS_ORIGINS=*                         # Quién puede llamar la API desde el navegador

# ============================================================
#  BASE DE DATOS (POSTGRES)
# ============================================================
DATABASE_URL=postgresql+psycopg://invoice:invoice@localhost:5432/invoices
#            │      │       │        │    │        │           │      │
#            │      │       │        │    │        │           │      └─ nombre BD
#            │      │       │        │    │        └───────────┴──── puerto (5432 = postgres)
#            │      │       │        │    └────────────────────── contraseña
#            │      │       │        └─────────────────────────── usuario
#            │      │       └────────────────────────driver de Python (psycopg3)
#            │      └────────────────diálgebra SQLAlchemy (postgresql)
#            └───────────────────────protocolo

# ============================================================
#  REDIS (la cola de mensajes)
# ============================================================
REDIS_URL=redis://localhost:6379/0
#          │      │        │     └─ base lógica de Redis (0-15)
#          │      │        └───────puerto (6379 = redis)
#          │      └───────────────host
#          └─────────────────────protocolo

# ============================================================
#  STORAGE (dónde se guardan los PDFs)
# ============================================================
STORAGE_PATH=./data/uploads     # carpeta local donde se persisten los archivos

# ============================================================
#  LÍMITE DE SUBIDA
# ============================================================
MAX_FILE_SIZE_MB=10             # tamaño máximo por archivo

# ============================================================
#  CELERY (la gestión de tareas)
# ============================================================
CELERY_TASK_TIMEOUT_SECONDS=600   # si una tarea tarda más, se decide que falló
CELERY_MAX_RETRIES=3              # cuántas veces se reintenta un fallo transitorio
CELERY_RETRY_BACKOFF_SECONDS=30   # espera base entre reintentos (exponencial: 30,60,120)

# ============================================================
#  OCR (leer texto de ESCANEOS cuando el PDF es una imagen)
# ============================================================
OCR_PROVIDER=local              # solo hay "local" por ahora
OCR_MIN_TEXT_CHARS_PER_PAGE=40  # si una página digital tiene menos de 40 chars,
                                # se asume que es un escaneo y se usa OCR
OCR_LANGUAGE=eng+spa            # idiomas del OCR (inglés+español)

# ============================================================
#  LLM / IA  👈  AQUÍ ESTÁ EL INTERRUPTOR
# ============================================================
LLM_EXECUTION=api               # api | local  ← EL INTERRUPTOR PRINCIPAL
LLM_PROVIDER=hybrid             # hybrid | rules | openai | mock
                                #  hybrid = reglas primero + fallback según LLM_EXECUTION
                                #  rules  = solo reglas (sin IA nunca)
                                #  openai = SIEMPRE el modelo de visión remoto
                                #  mock   = datos falsos para probar
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
#           URL del endpoint "OpenAI-compatible" de Gemini.
#           En modo local se ignora.
LLM_API_KEY=TU_API_KEY_AQUI     # la clave secreta. SOLO se usa en modo api.
LLM_MODEL=gemini-3.5-flash-lite # qué modelo remoto usar. Se ignora en modo local.
LLM_TIMEOUT_SECONDS=60          # tiempo máximo esperando la respuesta de la IA
LLM_TEMPERATURE=0.0             # 0.0 = la IA es lo más precisa/determinista posible
LLM_MAX_ATTEMPTS=3              # reintentos internos si la IA da una respuesta inválida
VISION_MAX_PAGES=4              # cuántas páginas del PDF le mostramos a la IA como imagen

# ============================================================
#  OCR LOCAL (SOLO útil si LLM_EXECUTION=local)
# ============================================================
LOCAL_OCR_ENGINE=vl             # vl | paddle | tesseract
                                #  vl        = PaddleOCR-VL (el más potente, recomendado)
                                #  paddle    = PP-OCR clásico (más rápido, menos calidad)
                                #  tesseract = respaldo clásico
LOCAL_OCR_LANG=es               # idioma del OCR local
```

### 8.3 Resumen rápido: solo tenés que tocar esto

**Modo API (con Gemini):**
```
LLM_EXECUTION=api
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_API_KEY=<tu clave de Gemini>
LLM_MODEL=gemini-3.5-flash-lite
```

Para sacar la API key de Gemini: entrá a https://aistudio.google.com/apikey , creá una (es gratis con cuota diaria), copiá la clave.

**Modo local (sin internet):**
```
LLM_EXECUTION=local
LOCAL_OCR_ENGINE=vl
LOCAL_OCR_LANG=es
```
(En local no hace falta key, ni URL, ni modelo.)

### 8.4 Después de editar el `.env`: recrear los contendenedores

Las variables se inyectan cuando arrancan los contenedores. No alcanza con reiniciar; hay que **recrear**:

```bash
podman-compose down
podman-compose build
podman-compose up -d
```

**Atención con un detalle tonto pero importante**: la versión de podman-compose acá NO soporta `build --quiet`. Si omitís el paso `build`, se usa la imagen vieja y tu cambio de `.env` no llega. Hacé los tres comandos.

### 8.5 Verificar que el cambio se aplicó

```bash
podman exec invoice-processor_worker_1 printenv LLM_EXECUTION   # debería decir "api" o "local"
podman logs invoice-processor_worker_1 2>&1 | grep -i "modo\|local\|hybrid\|fallback"
curl http://localhost:8000/health
```

---

## Parte 9 — Glosario de conceptos

| Término | Qué significa en este proyecto |
|---|---|
| **Puerto (Port)** | Una clase abstracta (ABC) que define un contrato: qué se puede hacer, sin cómo. |
| **Adaptador** | La implementación real de un puerto (ej. qué OCR concreto). |
| **Arquitectura hexagonal** | Dividir el sistema en núcleo (dominio) + aplicación + adaptadores, separados por interfaces. |
| **Inyección de dependencias** | Pasarle las dependencias a un objeto cuando se construye (en vez de que él las cree). Acá lo hace `container.py`. |
| **Raíz de composición** | Un único lugar donde se elige qué adaptador usa cada puerto. |
| **Celery** | Biblioteca que ejecuta tareas en segundo plano usando una cola. |
| **Redis** | Base de datos en memoria que acá sirve de cola de mensajes (broker). |
| **Worker** | Proceso de Celery que toma tareas de la cola y las ejecuta. |
| **Broker** | El "buzón" donde se dejan los mensajes (Redis). |
| **Backend** | Donde se guardan resultados de tareas (también Redis acá). |
| **Cola / Queue** | Lista de tareas pendientes. |
| **Docker Compose** | Definición en YAML de un conjunto de contenedores que corren juntos. |
| **Entidad** | Objeto del dominio con identidad propia y reglas internas (Invoice, Job...). |
| **Value Object** | Objeto inmutable que representa un valor (Money, FileType). |
| **Use Case (Caso de uso)** | Un paso del negocio que la aplicación orquesta. |
| **OCR** | Reconocimiento óptico: convertir imagen/escaneo en texto. |
| **Hybrid extraction** | Combina reglas deterministas + un fallback (IA/OCR) según hace falta. |
| **Polling** | Preguntar repetidamente "¿ya terminó?" (la UI lo hace cada 2s). |
| **Backoff exponencial** | Esperar 30s, luego 60s, 120s... entre reintentos. |
| **Retryable vs no** | Si un error es transitorio (se reintenta) o permanente (no sirve reintentar). |
| **Base de datos / ORM** | PostgreSQL (BD) + SQLAlchemy (mapea objetos ↔ filas). |

---

## Parte 10 — El mapa del código (dónde está cada cosa)

```
app/
├── domain/                     ← EL NÚCLEO (lógica pura, sin dependencias externas)
│   ├── entities/               ← Invoice, Supplier, Document, ProcessingJob
│   ├── value_objects/          ← Money, FileType, validaciones, enums
│   └── services/               ← PUERTOS: OCRProvider, InvoiceExtractor, DocumentStorage
│       └── invoice_validator.py ← reglas de negocio (matemática, fechas)
│
├── application/                ← ORQUESTACIÓN (casos de uso)
│   ├── services/               ← PUERTOS: UnitOfWork, TaskDispatcher
│   └── use_cases/              ← upload_invoice, process_invoice, consultas...
│
├── infrastructure/             ← ADAPTADORES (el "cómo" concreto)
│   ├── container.py            ← raíz de composición (une todo)
│   ├── celery_app/             ← Celery app, dispatcher, tasks, señales
│   ├── database/               ← modelos ORM + sesión SQLAlchemy
│   ├── repositories/           ← implementaciones SQLAlchemy de los puertos
│   ├── llm/                    ← extractores: rules, hybrid, openai, local_ocr, mock
│   ├── ocr/                    ← motores: tesseract, paddle, paddle-vl
│   ├── storage/                ← LocalDocumentStorage
│   ├── security/               ← headers, rate-limit, anti-SSRF
│   └── logging_setup.py        ← logging estructurado JSON
│
├── presentation/               ← WEB (FastAPI + Streamlit)
│   ├── api/                    ← routers, schemas, mappers, exception_handlers, deps, main
│   └── streamlit/              ← panel web (una página por vista)
│
└── config/                     ← Settings (toda la configuración del .env)
    └── settings.py
```

---

## Parte 11 — Ejercicios para afianzar (si querés)

1. **Encontrá el puerto**: buscá `class InvoiceExtractor(ABC)` y listá TODOS los archivos que lo implementan.
2. **Rastreá un flujo**: empieza en `presentation/api/routers/invoices.py` (upload) y seguí qué se llama hasta llegar a Celery.
3. **Cambiá el modo**: editá `.env`, recreá, y mirá los logs del worker para ver qué extractor se usó.
4. **Agregá un motor OCR**: implementá otro `OcrEngine` y agregalo a `build_ocr_engine`. Verás que no tocás nada más.
5. **Reproducí la fusión híbrida**: en `hybrid_extractor.py`, ¿qué pasa si las reglas hallan solo `total`? ¿Qué gana?

---

## Resumen final en 60 segundos

1. El sistema es una **fábrica**: entra (FastAPI) → agenda (Celery+Redis) → procesa (workers) → guarda (PostgreSQL) → consulta (Streamlit).
2. Usa **arquitectura hexagonal**: el negocio (dominio) está aislado tras **puertos** (interfaces), implementados por **adaptadores** que puedes cambiar sin romper nada.
3. El trabajo pesado corre **asíncrono (Celery)** para no congelar al usuario; el estado se guarda en BD y se reintenta cuando el error es transitorio.
4. La extracción es **híbrida**: reglas deterministas primero (gratis y exactas), e IA/OCR solo de respaldo.
5. Para cambiar entre **local y API**, tocás una sola variable: `LLM_EXECUTION`. Y tras editar el `.env`, **recreás los contenedores** (`down`, `build`, `up`).
