# Configuración — Detalle clase por clase

Complemento del §6 de `desglose_componentes.md` (`app/config/`). Toda configuración es environment-driven; solo la raíz de composición y los adaptadores importan este módulo.

---

## `settings.py`

### `class Settings(BaseSettings)` (pydantic-settings)
**Modela** toda la configuración runtime; cada campo mapea 1:1 con una variable de `.env`. Config: `case_sensitive=False`, `extra="ignore"` (variables ajenas no rompen).

Campos por bloque:

| Bloque | Campo | Default | Función |
|---|---|---|---|
| App | `app_name` | "Invoice Processing System" | Título API/UI. |
| | `environment` | development | Selecciona `.env` real vs ejemplo. |
| | `log_level` / `log_format` | INFO / json | Verbosidad y formato (json\|text). |
| | `cors_origins` | "*" | Orígenes CORS separados por coma. |
| BD | `database_url` | postgresql+psycopg://invoice:invoice@localhost:5432/invoices | DSN psycopg3. |
| Redis | `redis_url` | redis://localhost:6379/0 | Broker+backend Celery. |
| Storage | `storage_path` | ./data/uploads | Raíz del blob storage local. |
| Upload | `max_file_size_mb` | 10 | Cap de tamaño. |
| Celery | `celery_task_timeout_seconds` | 600 | Límite blando/duro de task. |
| | `celery_max_retries` / `celery_retry_backoff_seconds` | 3 / 30 | Política de reintentos. |
| OCR | `ocr_provider` | local | Proveedor del puerto OCR. |
| | `ocr_min_text_chars_per_page` | 40 | Umbral página-escaneo vs digital. |
| | `ocr_language` | eng+spa | Idiomas Tesseract. |
| LLM | `llm_provider` | hybrid | hybrid \| rules \| openai \| mock. |
| | `llm_execution` | api | **api \| local**: quién hace la escalada del híbrido. |
| | `llm_api_key` | SecretStr("") | Secreto blindado contra logs/reprs. |
| | `llm_model` | gemini-3.5-flash-lite | Modelo de visión remoto. |
| | `llm_base_url` | None | Endpoint OpenAI-compatible. |
| | `llm_timeout_seconds` / `llm_temperature` / `llm_max_attempts` | 60 / 0.0 / 3 | Transporte y determinismo. |
| | `vision_max_pages` | 4 (1..20) | Páginas rasterizadas por llamada. |
| OCR local | `local_ocr_engine` | vl | vl \| paddle \| tesseract. |
| | `local_ocr_lang` | es | Idioma del motor local. |

Validadores (`@field_validator`):
- `_validate_llm_base_url(mode="before")` — Anti-SSRF al arrancar: exige http(s) y rechaza IPs privadas/loopback/link-local y hosts internos → fail-fast en compose, no a mitad de un job.
- `_normalize_log_level` — Solo {DEBUG,INFO,WARNING,ERROR}.
- `_validate_llm_execution` — Solo {api, local} (el switch del híbrido).
- `_validate_local_ocr_engine` — Solo {vl, paddle, tesseract}.
- `_lowercase(mode="after")` — Normaliza log_format, llm_provider, ocr_provider y environment a minúsculas.

Propiedades derivadas:
- `max_file_size_bytes` — MB→bytes para el cap de upload.
- `is_production` — Atajo para comportamientos sensibles al entorno.

### `get_settings() -> Settings` *(decorada con `lru_cache`)*
Accesor cached a nivel proceso — punto de entrada único de la raíz de composición. Selección de dotenv por `ENVIRONMENT` (variable de proceso inyectada por docker-compose): `production` carga `.env`; cualquier otro valor carga `.env.example`. Las variables reales de entorno siempre ganan sobre cualquier archivo.
