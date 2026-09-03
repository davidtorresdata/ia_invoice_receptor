# Manual de usuario — Pruebas con archivos reales

> Guía práctica para levantar el sistema y probarlo con tus propias facturas
> (PDF, PNG, JPG). Todos los comandos se ejecutan desde la raíz del proyecto.
>
> **Docker o Podman, elige el tuyo**: los ejemplos usan `docker compose`.
> Con Podman sustituye por `podman-compose -f docker-compose.yml …`
> (y `make up` sigue funcionando igual). Instalación, equivalencias y
> solución de problemas: README §"Arranque con Podman".

---

## 1. Requisitos previos

| Requisito | Comprobar |
|---|---|
| Docker + Compose v2 **o** Podman + podman-compose ≥ 1.0.6 | `docker compose version` / `podman-compose --version` |
| `curl` (opcional, para CLI) | `curl --version` |
| ~2 GB libres en disco (imágenes + volúmenes) | `df -h` |

No necesitas Python local: todo corre en contenedores.

---

## 2. Levantar el stack completo

```bash
cp .env.example .env     # 1ª vez: configuración por defecto lista para usar
make up                  # build + postgres + redis + migraciones + api + worker + ui
docker compose ps        # todo debe estar "running/healthy"
curl http://localhost:8000/health
# -> {"status":"ok","version":"0.1.0","components":{"database":"up","redis":"up"}}
#    (503 "degraded" si BD o Redis no responden)
```

El servicio `migrate` crea las tablas automáticamente antes de levantar api/worker.
Si algo falla al arrancar: `make logs` (muestra api y worker en vivo).

**URLs disponibles**

| Interfaz | URL | Uso |
|---|---|---|
| API REST | http://localhost:8000 | integración / curl |
| Swagger interactivo | http://localhost:8000/docs | probar endpoints desde el navegador |
| Streamlit | http://localhost:8501 | subir facturas con formulario gráfico |

---

## 3. Elige el modo de extracción (importante)

Lo que verás en los datos extraídos depende de `LLM_PROVIDER` en tu `.env`:

### Modo `mock` (por defecto — sin coste, sin internet)
- Acepta cualquier factura real y procesa TODO el pipeline de verdad
  (OCR incluido), pero los datos estructurados (número, proveedor, importes)
  son **sintéticos y reproducibles**: derivan del hash del texto del documento.
- Ideal para verificar infraestructura, estados, validación y dashboard.

### Modo `openai` (extracción real del contenido)
Edita `.env`, luego `make down && make up` para aplicar:

```env
LLM_PROVIDER=openai
LLM_API_KEY=sk-...tu-clave...
LLM_MODEL=gpt-4o-mini          # o el modelo que prefieras
# LLM_BASE_URL=https://api.openai.com/v1   # opcional (OpenRouter, Ollama, Azure…)
```

> La clave nunca aparece en logs ni respuestas (`SecretStr`). Si la API falla,
> el job reintenta con backoff y termina FAILED con mensaje claro.

---

## 4. Prepara archivos de prueba válidos

**Reglas de aceptación** (las valida `FileType.validate`):
- Extensiones: `.pdf`, `.png`, `.jpg`, `.jpeg`
- Tamaño máximo: 10 MB (`MAX_FILE_SIZE_MB`)
- El contenido debe coincidir con la extensión (se inspeccionan *magic bytes*,
  no se confía en el nombre ni en el MIME declarado)

**Qué funciona mejor**

| Tipo de archivo | Resultado esperado |
|---|---|
| PDF nativo (descargado/generado, con texto seleccionable) | Extracción limpia vía texto embebido — rápida |
| PDF escaneado (solo imagen) | Cada página se rasteriza a 200 DPI y pasa por Tesseract (`eng+spa`) — más lento pero funcional |
| Foto nítida y recta de una factura (JPG/PNG) | Va directa a Tesseract |

**Consejos para fotos/escaneos:** buena luz, sin sombras, documento plano y
completo, texto legible a simple vista. Tesseract no hace magia con borrosos.

### Genera PDFs de prueba realistas (opcional)

```bash
python3 - <<'EOF'
import pymupdf

doc = pymupdf.open()
page = doc.new_page()
page.insert_text((72, 72), (
    "FACTURA\n"
    "Numero: FAC-2026-0042\n"
    "Fecha: 2026-01-10    Vencimiento: 2026-02-09\n"
    "Proveedor: Suministros Industriales SL   NIF: B87654321\n\n"
    "Descripcion              Cant   P.U.      Total\n"
    "Mantenimiento anual       1     480.00    480.00\n"
    "Horas soporte             8      45.50    364.00\n\n"
    "Base: 844.00   IVA(21%): 177.24   TOTAL: 1021.24 EUR\n"
), fontsize=11)
open("prueba-nativa.pdf", "wb").write(doc.tobytes())
doc.close()

# Variante ESCANEADA (sin capa de texto) para forzar Tesseract:
src = pymupdf.open("prueba-nativa.pdf")
pix = src[0].get_pixmap(dpi=150)                 # rasterizamos la pagina
scan = pymupdf.open()
page = scan.new_page(width=pix.width, height=pix.height)
page.insert_image(page.rect, pixmap=pix)
open("prueba-escaneada.pdf", "wb").write(scan.tobytes())
print("Creados prueba-nativa.pdf y prueba-escaneada.pdf")
EOF
```

---

## 5. Subir archivos — tres formas

### A. Terminal (curl)

```bash
RESP=$(curl -s -F "file=@mi-factura.pdf" \
            http://localhost:8000/api/v1/invoices/upload)
echo "$RESP"
# {"document_id":"…","job_id":"…","filename":"mi-factura.pdf",
#  "status":"PENDING","poll_url":"/api/v1/jobs/xxxxxxxx"}
```

Guarda el `job_id` de la respuesta para el paso 6. También puedes subir varios:

```bash
for f in facturas/*.pdf; do curl -s -F "file=@$f" http://localhost:8000/api/v1/invoices/upload | head -c 80; echo; done
```

### B. Swagger UI
1. Abre http://localhost:8000/docs
2. `POST /invoices/upload` → *Try it out* → *Choose file* → *Execute*
3. Copia `job_id` de la respuesta 202.

### C. Interfaz Streamlit (la más cómoda)
1. Abre http://localhost:8501 → página **Subir factura**
2. Arrastra el archivo y confirma
3. La página hace polling automático y muestra el estado hasta COMPLETED/FAILED.

---

## 6. Consultar resultados

```bash
JOB=<job_id de la respuesta>

# Estado del pipeline (repite hasta "COMPLETED"; normalmente tarda segundos)
curl -s http://localhost:8000/api/v1/jobs/$JOB
# {"id":"…","status":"COMPLETED","attempts":1,"invoice_id":"…","started_at":…}

INVOICE=<invoice_id del job>

# Factura completa: proveedor, líneas, totales y reporte de validación
curl -s http://localhost:8000/api/v1/invoices/$INVOICE | python3 -m json.tool
```

Campos interesantes de la respuesta:

| Campo | Significado |
|---|---|
| `validation_status` | `VALID` (aritmética cuadra) / `INVALID` (ver `validation_report.issues`) |
| `validation_report` | Lista trazable `{code, severity, message, field}` — p. ej. `math.total_mismatch` si el total no cuadra |
| `raw_extraction` | JSON crudo devuelto por el LLM (auditoría) |
| `items[]` | Líneas con cantidad, precio unitario e importes |

Búsquedas y métricas:

```bash
curl -s "http://localhost:8000/api/v1/invoices?search=B87654321"          # por NIF/nº
curl -s "http://localhost:8000/api/v1/invoices?validation_status=VALID&limit=50"
curl -s http://localhost:8000/api/v1/dashboard/stats                      # contadores globales
```

En Streamlit: páginas **Facturas**, **Detalle** y **Dashboard** reflejan lo mismo
gráficamente.

---

## 7. Observar el proceso por dentro

```bash
make logs                       # api + worker en vivo (Ctrl+C para salir)
docker compose logs worker | grep -E "Pipeline|Task"
# Podman: podman-compose -f docker-compose.yml logs -f api worker
```

Verás etapas como `Pipeline: started`, `text extracted {method: embedded-text|tesseract}`,
`persisted invoice {validation: VALID}` y `Task finished successfully`.

### Rastrear cualquier error hasta su módulo

Cada línea de log (api, worker y streamlit) incluye su origen exacto:
`module`, `file`, `function`, `line`. Las excepciones añaden un bloque
estructurado con tipo, módulo Python, punto exacto donde se lanzó (`origin`)
y traceback completo. Ejemplos de consulta:

```bash
docker compose logs api | jq 'select(.level=="ERROR" or .level=="CRITICAL")'
# -> {"level":"ERROR","module":"app.presentation.api.exception_handlers",
#     "file":"app/presentation/api/exception_handlers.py","line":…,
#     "error_code":"invalid_file", "path":"/api/v1/invoices/upload",
#     "exception":{"type":"InvalidFileError","python_module":"app.domain.exceptions",
#                  "origin":{"file":"app/domain/services/file_type.py", …}}}

# errores del worker con su tarea:
docker compose logs worker | jq 'select(.task != null) | {task, task_id, level, message}'

# solo los no capturados (bugs reales):
docker compose logs | jq 'select(.message | startswith("Uncaught exception"))'
```

Los errores de dominio (400/404/422) se registran en WARNING; fallos de
servidor (5xx), tareas Celery fallidas y excepciones no capturadas en
ERROR/CRITICAL. Con `LOG_FORMAT=text`: `2026-… ERROR [app.mod :: func:42] msg`.

Los blobs quedan en el volumen `uploads` (dentro del contenedor
`/app/data/uploads/YYYY/MM/…`) y los datos en el volumen `pgdata`
(sobreviven a `make down`; se borran con `docker compose down -v`).

---

## 8. Prueba también los casos de error (contrato garantizado)

| Prueba | Comando | Respuesta esperada |
|---|---|---|
| Extensión no permitida | `curl -F "file=@nota.txt" …/upload` | **400** `{"error":{"code":"invalid_file",…}}` |
| Contenido falsificado (`.pdf` que no lo es) | renombra un .txt a .pdf y súbelo | **400** `invalid_file` ("content does not match signatures") |
| Excede el límite | `head -c 11M /dev/urandom > gordo.pdf; súbelo` | **413** `file_too_large` |
| Fichero vacío | `touch vacio.pdf; súbelo` | **400** `empty_file` / `invalid_file` |
| Job inexistente | `curl …/api/v1/jobs/00000000-0000-0000-0000-000000000000` | **404** `job_not_found` |
| PDF corrupto (aceptado, falla en OCR) | trunca un PDF real (`head -c 200 ok.pdf > roto.pdf`) | 202 → job **FAILED** con mensaje; el pipeline nunca cuelga |

Nota: los errores permanentes no consumen reintentos; los transitorios
(p. ej. caída momentánea de OpenAI) reintentan solo: backoff 30·2ⁿ, máx 3,
y el job permanece PROCESSING entre intentos.

---

## 9. Limpieza

```bash
make down                    # para contenedores (datos persisten)
docker compose down -v       # ADVERTENCIA: borra también BD + blobs
rm prueba-*.pdf mi-factura.pdf   # ficheros locales de prueba
```

Para "empezar de cero" conservando el entorno:
`docker compose restart worker api` reinicia el procesamiento pendiente.

---

## 10. Problemas frecuentes

| Síntoma | Causa probable | Solución |
|---|---|---|
| `make up` falla copiando README.md | repo incompleto | ya corregido: existe `README.md`; haz `git pull` / rebuild |
| `/health` devuelve 503 `degraded` | Postgres/Redis aún arrancando | espera ~15 s y reintenta; `docker compose ps` para ver healthchecks |
| Job queda PENDING mucho tiempo | worker caído | `docker compose ps`; `make logs`; `docker compose restart worker` |
| `FAILED - Processing timed out` | PDF gigantesco o LLM lento | sube `CELERY_TASK_TIMEOUT_SECONDS` en `.env` |
| Datos extraídos no coinciden con mi factura | estás en modo `mock` | cambia a `LLM_PROVIDER=openai` (§3) |
| OCR produce texto ilegible | escaneo de baja calidad | reescanea ≥ 200 DPI; fotos rectas y con luz |
| Puerto 8000/8501 ocupado | otro servicio local | cambia el mapeo de puertos en `docker-compose.yml` |
| Quiero más throughput | — | `docker compose up -d --scale worker=4` |

---

## 11. Chuleta de una línea a línea

```bash
cp .env.example .env && make up && sleep 20 && curl localhost:8000/health
curl -s -F "file=@mi-factura.pdf" localhost:8000/api/v1/invoices/upload
curl -s localhost:8000/api/v1/jobs/<job_id>
curl -s localhost:8000/api/v1/invoices/<invoice_id> | python3 -m json.tool
make logs   # y para parar: make down
```
