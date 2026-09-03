# Guía: Cambio entre IA local y IA por API

El sistema soporta dos modos de extracción híbrida. El punto de cambio es una sola variable de entorno.

---

## El interruptor

```
LLM_EXECUTION=api    → modelo de visión remoto (Gemini/OpenAI)
LLM_EXECUTION=local  → PaddleOCR-VL + reglas, 100% offline
```

La variable vive en el archivo `.env` en la raíz del proyecto.

---

## Cómo hacerlo

### 1. Editar `.env`

Abrir el archivo y buscar/crear las siguientes líneas según el modo deseado:

**Modo API (remoto):**

```env
LLM_EXECUTION=api
LLM_PROVIDER=hybrid
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_API_KEY=tu-api-key-aquí
LLM_MODEL=gemini-3.5-flash-lite
LLM_TIMEOUT_SECONDS=300
LOCAL_OCR_ENGINE=vl
LOCAL_OCR_LANG=es
```

**Modo local (sin red):**

```env
LLM_EXECUTION=local
LLM_PROVIDER=hybrid
LOCAL_OCR_ENGINE=vl
LOCAL_OCR_LANG=es
```

En modo local las variables `LLM_BASE_URL`, `LLM_API_KEY` y `LLM_MODEL` se ignoran; el sistema nunca sale de la máquina.

### 2. Recrear los contenedores

Las variables de entorno se inyectan al arrancar los contenedores. Un simple reinicio no basta:

```bash
podman-compose down
podman-compose build
podman-compose up -d
```

> Nota: podman-compose **no soporta `build --quiet`** en esta versión. Si omitís `build` se usa la imagen anterior; los cambios en `.env` no llegarían al worker.

### 3. Verificar que el cambio tomó efecto

```bash
# Estado de los contenedores
podman ps --format "{{.Names}} {{.Status}}"

# Variable en el worker
podman exec invoice-processor_worker_1 printenv LLM_EXECUTION

# Salud de la API
curl http://localhost:8000/health

# Logs del worker (debe mostrar qué extractor se usó)
podman logs invoice-processor_worker_1 2>&1 | grep -i "modo\|local\|api\|hybrid\|vl\|fallback"
```

---

## Variables relevantes

| Variable | Valores | Efecto |
|---|---|---|
| `LLM_EXECUTION` | `api` \| `local` | **Interruptor principal**. Decide qué hace el fallback híbrido cuando las reglas no alcanzan. |
| `LLM_PROVIDER` | `hybrid` \| `rules` \| `openai` \| `mock` | `hybrid` (default) usa reglas primero y escala según LLM_EXECUTION. `rules` no escala nunca. `openai` siempre va al LLM remoto. `mock` para tests. |
| `LLM_BASE_URL` | URL pública | Endpoint OpenAI-compatible. Se ignora en modo local. Valida anti-SSRF al arrancar. |
| `LLM_API_KEY` | tu API key | Solo necesaria en modo api. Blindada con SecretStr. |
| `LLM_MODEL` | `gemini-3.5-flash-lite` u otro | Modelo remoto. Se ignora en modo local. |
| `LOCAL_OCR_ENGINE` | `vl` \| `paddle` \| `tesseract` | Motor OCR en modo local. `vl` usa PaddleOCR-VL (mejor calidad). |
| `LOCAL_OCR_LANG` | `es`, `en`, etc. | Idioma del motor OCR. |
| `LLM_TIMEOUT_SECONDS` | 60 (default) | Timeout de la llamada al LLM remoto. Se ignora en modo local. |

---

## Flujo de cada modo

**Modo API:**

```
PDF → texto embebido → reglas → ¿faltan campos?
                                  → Gemini/OpenAI vision call → fusión → resultado
```

**Modo local:**

```
PDF → texto embebido → reglas → ¿faltan campos?
                                  → renderiza páginas → PaddleOCR-VL transcribe
                                  → reglas otra vez sobre texto fusionado → resultado
```

En ambos modos, si las reglas resuelven todo el documento (factura electrónica digital) la escalada no se usa y la respuesta es instantánea.

---

## Cambio rápido (sin reconstruir imagen)

Si solo cambiaste variables de entorno (no archivos `.py`), podés recrear sin rebuild:

```bash
podman-compose down && podman-compose up -d
```

Si cambiaste código (`.py`), necesitás el build completo:

```bash
podman-compose down && podman-compose build && podman-compose up -d
```
