"""Tests for the hybrid extraction flow (rules -> vision/OCR escalation)."""

import base64
import json
from decimal import Decimal
from typing import ClassVar

import httpx
import pytest
from pydantic import ValidationError

from app.domain.exceptions import LLMExtractionError, StorageError
from app.domain.value_objects.enums import DocumentType
from app.infrastructure.llm.hybrid_extractor import HybridInvoiceExtractor
from app.infrastructure.llm.openai_extractor import OpenAICompatibleInvoiceExtractor
from app.infrastructure.llm.page_renderer import render_page_images
from tests.fakes import make_valid_extraction


class StubExtractor:
    """Minimal double for wiring assertions."""

    def __init__(self, result=None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls: list[tuple[str, list[bytes] | None]] = []

    def extract(self, document_text: str, images=None):
        self.calls.append((document_text, list(images) if images else None))
        if self.exc is not None:
            raise self.exc
        return self.result


# --------------------------------------------------------------------- hybrid
class TestHybridRouting:
    def test_rules_success_skips_vision(self):
        primary = StubExtractor(result=make_valid_extraction())
        fallback = StubExtractor()
        hybrid = HybridInvoiceExtractor(primary=primary, fallback=fallback)

        data = hybrid.extract("texto", images=[b"png"])

        assert data is not None
        assert fallback.calls == []

    def test_pattern_miss_escalates_with_images(self):
        primary = StubExtractor(exc=LLMExtractionError("No se encontró el total"))
        fallback = StubExtractor(result=make_valid_extraction())
        hybrid = HybridInvoiceExtractor(primary=primary, fallback=fallback)

        hybrid.extract("ruido", images=[b"page1", b"page2"])

        assert len(fallback.calls) == 1
        assert fallback.calls[0][1] == [b"page1", b"page2"]

    def test_without_fallback_reraises_original_error(self):
        original = LLMExtractionError("sin patron")
        hybrid = HybridInvoiceExtractor(primary=StubExtractor(exc=original), fallback=None)

        with pytest.raises(LLMExtractionError, match="sin patron"):
            hybrid.extract("ruido")

    def test_non_llm_errors_propagate_without_escalation(self):
        primary = StubExtractor(exc=StorageError("blob perdido"))
        fallback = StubExtractor()
        hybrid = HybridInvoiceExtractor(primary=primary, fallback=fallback)

        with pytest.raises(StorageError):
            hybrid.extract("ruido")

        assert fallback.calls == []


# ------------------------------------------------------- vision request shape
def _canned_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(payload)}}]
    })


VALID_PAYLOAD = {
    "number": "FE364",
    "date": "2026-06-15",
    "due_date": None,
    "currency": "COP",
    "subtotal": 136135,
    "tax": 25865,
    "total": 162000,
    "supplier": {"name": "TRADUCTORES.CO S.A.S.", "tax_id": "900755117-8"},
    "items": [{"description": "Traduccion", "quantity": 1,
               "unit_price": 136135, "tax": 25865, "total": 136135}],
}


class TestVisionRequestBuilding:
    def _extractor(self, captured: dict) -> OpenAICompatibleInvoiceExtractor:
        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return _canned_response(VALID_PAYLOAD)

        return OpenAICompatibleInvoiceExtractor(
            api_key="sk-test",
            model="some-vision-model",
            transport=httpx.MockTransport(handler),
        )

    def test_images_sent_as_base64_data_urls(self):
        captured: dict = {}
        extractor = self._extractor(captured)

        data = extractor.extract("texto ocr", images=[b"\x89PNG-fake"])

        assert data.number == "FE364"
        content = captured["body"]["messages"][1]["content"]
        assert isinstance(content, list)
        kinds = {part["type"] for part in content}
        assert kinds == {"text", "image_url"}
        image_part = next(p for p in content if p["type"] == "image_url")
        decoded = base64.b64decode(
            image_part["image_url"]["url"].removeprefix("data:image/png;base64,")
        )
        assert decoded == b"\x89PNG-fake"

    def test_text_only_keeps_plain_string_content(self):
        captured: dict = {}
        extractor = self._extractor(captured)

        extractor.extract("solo texto")

        content = captured["body"]["messages"][1]["content"]
        assert isinstance(content, str)
        assert "solo texto" in content

    def test_invalid_payload_raises_llm_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            bad = dict(VALID_PAYLOAD, currency="pesos")
            return _canned_response(bad)

        extractor = OpenAICompatibleInvoiceExtractor(
            api_key="sk-test", model="some-vision-model",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises((LLMExtractionError, ValidationError)):
            extractor.extract("texto")


# ------------------------------------------------------------------- renderer
class TestPageRenderer:
    def _pdf_bytes(self, pages: int = 2) -> bytes:
        import pymupdf

        doc = pymupdf.open()
        for _ in range(pages):
            doc.new_page(width=200, height=100)
        payload = doc.tobytes()
        doc.close()
        return payload

    def test_pdf_renders_png_pages(self):
        pages = render_page_images(self._pdf_bytes(), DocumentType.PDF)
        assert len(pages) >= 1
        assert all(page.startswith(b"\x89PNG") for page in pages)

    def test_max_pages_respected(self):
        pages = render_page_images(self._pdf_bytes(pages=5), DocumentType.PDF, max_pages=2)
        assert len(pages) == 2

    def test_image_documents_pass_through(self):
        blob = b"\x89PNG-image-bytes"
        assert render_page_images(blob, DocumentType.IMAGE) == [blob]

    def test_corrupted_pdf_degrades_to_empty(self):
        assert render_page_images(b"%PDF-corrupto", DocumentType.PDF) == []


class TestKeylessLocalServer:
    def test_empty_api_key_omits_auth_header(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            return _canned_response(VALID_PAYLOAD)

        extractor = OpenAICompatibleInvoiceExtractor(
            api_key="", model="some-vision-model",
            base_url="http://localhost:8765/v1",
            transport=httpx.MockTransport(handler),
        )
        extractor.extract("texto")
        assert captured["auth"] is None

    def test_key_present_sends_bearer(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            return _canned_response(VALID_PAYLOAD)

        extractor = OpenAICompatibleInvoiceExtractor(
            api_key="sk-abc", model="some-vision-model",
            base_url="http://localhost:8765/v1",
            transport=httpx.MockTransport(handler),
        )
        extractor.extract("texto")
        assert captured["auth"] == "Bearer sk-abc"


class TestJsonModeFallback:
    def test_400_mentioning_response_format_retries_without_it(self):
        calls: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            if "response_format" in body:
                return httpx.Response(400, json={"error": {"message": "response_format is not supported"}})
            return _canned_response(VALID_PAYLOAD)

        extractor = OpenAICompatibleInvoiceExtractor(
            api_key="k", model="gemini-flash-latest",
            base_url="https://generativelanguage.googleapis.com/openai/v1",
            transport=httpx.MockTransport(handler),
        )
        data = extractor.extract("texto")
        assert data.number == "FE364"
        assert len(calls) == 2
        assert "response_format" not in calls[1]


# ------------------------------------------------------- field-level merge
PARTIAL_TEXT = (
    "FACTURA ELECTRONICA DE VENTA\n"
    " FE364\n"
    "Fecha de creacion: 15/06/2026\n"
    "Subtotal\n$136.135\nIva (19.00%)\n$25.866\nTotal\n$162.000\n"
)

GEMINI_PAYLOAD_DIFFERENT = dict(
    VALID_PAYLOAD,
    number="XX-999",
    date="2030-01-01",
    subtotal=100, tax=10, total=110,
    supplier={"name": "Gemini S.A.S.", "tax_id": "999999999"},
)


class TestFieldLevelMerge:
    def test_rules_partial_error_carries_found_fields(self):
        from app.domain.exceptions import PartialExtractionError
        from app.infrastructure.llm.rules_extractor import RulesInvoiceExtractor

        with pytest.raises(PartialExtractionError) as excinfo:
            RulesInvoiceExtractor().extract(PARTIAL_TEXT)
        assert excinfo.value.partial_data["number"] == "FE364"
        assert excinfo.value.partial_data["issue_date"] == "2026-06-15"
        # Labels and amounts sit on separate lines, but the amount patterns
        # tolerate whitespace between label and value, so the monetary trio
        # IS recovered from this layout (total_simple covers bare "Total").
        assert excinfo.value.partial_data["tax"] in ("25866", "25865", "25866.00", "25865.00")
        assert Decimal(excinfo.value.partial_data["total"]) == 162000
        assert excinfo.value.missing_fields == ["supplier"]

    def test_merge_fills_missing_fields_from_vision(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _canned_response(GEMINI_PAYLOAD_DIFFERENT)

        fallback = OpenAICompatibleInvoiceExtractor(
            api_key="k", model="gemini-flash-latest",
            transport=httpx.MockTransport(handler),
        )
        hybrid = HybridInvoiceExtractor(
            primary=__import__("app.infrastructure.llm.rules_extractor", fromlist=["RulesInvoiceExtractor"]).RulesInvoiceExtractor(),
            fallback=fallback,
        )

        data = hybrid.extract(PARTIAL_TEXT, images=[b"page"])

        assert data.number == "FE364"                      # reglas gana
        assert data.issue_date.isoformat() == "2026-06-15"  # reglas gana
        assert data.supplier.name == "Gemini S.A.S."       # faltante -> vision
        assert data.subtotal == Decimal("136134")          # derivado: total - tax (redondeo del doc)
        assert data.tax == Decimal("25866")                # reglas
        assert data.total == Decimal("162000")             # reglas

    def test_partial_without_fallback_reraises(self):
        from app.domain.exceptions import PartialExtractionError
        from app.infrastructure.llm.rules_extractor import RulesInvoiceExtractor

        hybrid = HybridInvoiceExtractor(primary=RulesInvoiceExtractor(), fallback=None)
        with pytest.raises(PartialExtractionError):
            hybrid.extract(PARTIAL_TEXT)


    def test_merge_keeps_rules_totals_over_vision(self):
        """Symmetric merge: when rules DO find the monetary trio they win."""
        from app.infrastructure.llm.rules_extractor import RulesInvoiceExtractor

        text = (
            "FACTURA ELECTRONICA DE VENTA\n"
            " FE777\n"
            "Fecha de creacion: 15/06/2026\n"
            "Subtotal: $50.000\n"
            "Iva: $0\n"
            "TOTAL A PAGAR: $50.000\n"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return _canned_response(GEMINI_PAYLOAD_DIFFERENT)

        fallback = OpenAICompatibleInvoiceExtractor(
            api_key="k", model="gemini-flash-latest",
            transport=httpx.MockTransport(handler),
        )
        hybrid = HybridInvoiceExtractor(
            primary=RulesInvoiceExtractor(), fallback=fallback,
        )

        data = hybrid.extract(text, images=[b"page"])

        assert data.number == "FE777"                      # reglas
        assert data.supplier.name == "Gemini S.A.S."       # faltante -> vision
        assert data.subtotal == Decimal("50000")           # reglas GANAN
        assert data.tax == Decimal("0")                    # reglas
        assert data.total == Decimal("50000")              # reglas


class TestRateLimitHandling:
    def test_429_waits_server_advised_time(self, monkeypatch):
        from app.infrastructure.llm import openai_extractor as mod

        sleeps: list[float] = []
        monkeypatch.setattr(mod.time, "sleep", sleeps.append)

        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    429,
                    text='{"error":{"message":"quota... Please retry in 12.5s."}}',
                )
            return _canned_response(VALID_PAYLOAD)

        extractor = OpenAICompatibleInvoiceExtractor(
            api_key="k", model="m", transport=httpx.MockTransport(handler),
        )
        data = extractor.extract("texto")
        assert data.number == "FE364"  # payload de ejemplo del módulo
        assert calls["n"] == 2
        assert sleeps and sleeps[0] >= 14.0, sleeps


# ------------------------------------------------------- LLM_EXECUTION switch
class TestExecutionSwitch:
    def _hybrid(self, **overrides):
        from app.config.settings import Settings
        from app.infrastructure.llm import build_invoice_extractor

        defaults: dict = {
            "llm_provider": "hybrid",
            "llm_execution": "local",
        }
        defaults.update(overrides)
        return build_invoice_extractor(Settings(**defaults))

    def test_local_mode_wires_pp_ocr_rules(self):
        from app.infrastructure.llm.local_ocr_extractor import LocalOCRInvoiceExtractor
        from app.infrastructure.ocr.engines import PaddleOCRVLEngine

        hybrid = self._hybrid()
        assert isinstance(hybrid, HybridInvoiceExtractor)
        fallback = hybrid._fallback
        assert isinstance(fallback, LocalOCRInvoiceExtractor)
        assert isinstance(fallback._engine, PaddleOCRVLEngine)

    def test_local_engine_selection_paddle_and_tesseract(self):
        from app.config.settings import Settings
        from app.infrastructure.llm import build_invoice_extractor
        from app.infrastructure.llm.local_ocr_extractor import LocalOCRInvoiceExtractor
        from app.infrastructure.ocr.engines import (
            PaddleOCREngine,
            TesseractLinesEngine,
        )

        for engine_name, engine_cls in [
            ("paddle", PaddleOCREngine),
            ("tesseract", TesseractLinesEngine),
        ]:
            hybrid = build_invoice_extractor(Settings(
                llm_provider="hybrid", llm_execution="local",
                local_ocr_engine=engine_name,
            ))
            assert isinstance(hybrid._fallback, LocalOCRInvoiceExtractor)
            assert isinstance(hybrid._fallback._engine, engine_cls)

    def test_invalid_engine_fails_fast(self):
        import pydantic

        from app.config.settings import Settings

        with pytest.raises(pydantic.ValidationError):
            Settings(local_ocr_engine="bogus")

    def test_api_mode_keeps_remote_endpoint(self):
        hybrid = self._hybrid(
            llm_execution="api",
            llm_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            llm_api_key="secret",
            llm_model="gemini-flash-latest",
        )
        fallback = hybrid._fallback
        assert fallback is not None
        assert fallback._model == "gemini-flash-latest"
        assert "generativelanguage" in str(fallback._client.base_url)
        assert fallback._client.headers.get("Authorization") == "Bearer secret"

    def test_switch_changes_adapter_class_not_port(self):
        from app.domain.services.invoice_extractor import InvoiceExtractor
        from app.infrastructure.llm.openai_extractor import OpenAICompatibleInvoiceExtractor

        local = self._hybrid()._fallback
        remote = self._hybrid(
            llm_execution="api",
            llm_base_url="https://api.x.ai/v1",
            llm_api_key="k",
            llm_model="some-vl",
        )._fallback
        # Ambos implementan el mismo puerto (arquitectura intacta);
        # el switch solo cambia el adaptador de infraestructura.
        assert isinstance(local, InvoiceExtractor)
        assert isinstance(remote, InvoiceExtractor)
        assert not isinstance(local, OpenAICompatibleInvoiceExtractor)
        assert isinstance(remote, OpenAICompatibleInvoiceExtractor)

    def test_invalid_execution_fails_fast(self):
        import pydantic

        from app.config.settings import Settings

        with pytest.raises(pydantic.ValidationError):
            Settings(llm_execution="bogus")


class TestLocalOCRAdapter:
    """PP-OCR -> reglas: el parser es el mismo, cambia la fuente de texto."""

    OCR_LINES: ClassVar[list[str]] = [
        "TRADUCTORES.CO S.A.S.",
        "NIT 900755117-8",
        "Factura Electronica de Venta No. FE364",
        "Fecha de Emision: 15/06/2026",
        "Subtotal: $136135.00",
        "Impuestos IVA 19%: $25865.00",
        "TOTAL A PAGAR: $162000.00",
    ]

    class _FakeEngine:
        def __init__(self, lines=None):
            self._lines = lines
            self.calls = 0

        def lines(self, image_bytes):
            self.calls += 1
            source = self._lines if self._lines is not None else TestLocalOCRAdapter.OCR_LINES
            return list(source)

    def _adapter(self, engine=None):
        from app.infrastructure.llm.local_ocr_extractor import LocalOCRInvoiceExtractor

        return LocalOCRInvoiceExtractor(engine=engine or self._FakeEngine())

    def test_ocr_lines_are_parsed_by_rules(self):
        data = self._adapter().extract("texto embebido ruidoso", images=[b"png"])
        assert data.number == "FE364"
        assert int(data.total) == 162000
        assert data.supplier.tax_id == "900755117-8"

    def test_no_images_raises_extraction_error(self):
        from app.domain.exceptions import LLMExtractionError

        with pytest.raises(LLMExtractionError, match="renderizadas"):
            self._adapter().extract("texto")

    def test_empty_ocr_raises_extraction_error(self):
        from app.domain.exceptions import LLMExtractionError

        engine = self._FakeEngine(lines=["   ", ""])
        with pytest.raises(LLMExtractionError, match="hallaron campos"):
            self._adapter(engine).extract("t", images=[b"png"])

    def test_partial_result_propagates_with_payload(self):
        from app.domain.exceptions import PartialExtractionError

        partial_engine = self._FakeEngine(lines=[
            "Factura No. FE777",
            "TOTAL A PAGAR: $50000",
        ])
        with pytest.raises(PartialExtractionError) as excinfo:
            self._adapter(partial_engine).extract("t", images=[b"png"])
        assert excinfo.value.partial_data.get("number") == "FE777"

    def test_dual_source_fusion_builds_full_invoice(self):
        """Parcial del texto embebido + parcial del OCR -> extraccion completa."""
        embedded = (
            "Factura Electrónica De Venta No. FE3077\n"
            "Moneda: COP\n"
            "Fecha de Emisión: 18/07/2026 10:43 AM\n"
            "Emisor: DIVERS TEAM S.A.S\n"
        )
        ocr_engine = self._FakeEngine(lines=[
            "DIVERS TEAM S.A.S",
            "NIT 901.454.721-2",
            "Subtotal $100000",
            "Iva (19.00%) $19000",
            "Total $119000",
        ])
        data = self._adapter(ocr_engine).extract(embedded, images=[b"png"])
        assert data.number == "FE3077"
        assert int(data.total) == 119000
        assert data.supplier.name == "DIVERS TEAM S.A.S"

    def test_amount_trio_stays_atomic_from_ocr_owner(self):
        """Si el OCR halla el total, el trio completo viene del OCR."""
        from app.domain.exceptions import PartialExtractionError

        partial_engine = self._FakeEngine(lines=[
            "DIVERS TEAM S.A.S",
            "NIT 901.454.721-2",
            "Subtotal $100000",
            "Iva (19.00%) $19000",
            "Total $119000",
        ])
        with pytest.raises(PartialExtractionError) as excinfo:
            self._adapter(partial_engine).extract("t", images=[b"png"])
        amounts = excinfo.value.partial_data
        assert int(Decimal(amounts["total"])) == 119000
        assert int(Decimal(amounts["subtotal"])) == 100000
        assert int(Decimal(amounts["tax"])) == 19000


class TestEngineFactory:
    def test_unknown_engine_rejected(self):
        import pydantic

        from app.config.settings import Settings
        from app.infrastructure.ocr.engines import build_ocr_engine

        with pytest.raises(ValueError, match="local_ocr_engine"):
            build_ocr_engine("nope", "es")
        with pytest.raises(pydantic.ValidationError):
            Settings(local_ocr_engine="nope")


class TestReasoningFallback:
    def test_empty_content_falls_back_to_reasoning_field(self):
        from app.infrastructure.llm.openai_extractor import OpenAICompatibleInvoiceExtractor

        response = httpx.Response(200, json={
            "choices": [{"message": {"content": "", "reasoning": json.dumps(VALID_PAYLOAD)}}]
        })

        def handler(request: httpx.Request) -> httpx.Response:
            return response

        extractor = OpenAICompatibleInvoiceExtractor(
            api_key="k", model="m", transport=httpx.MockTransport(handler),
        )
        assert extractor.extract("texto").number == "FE364"

    def test_json_inside_prose_is_recovered(self):
        from app.infrastructure.llm.openai_extractor import OpenAICompatibleInvoiceExtractor

        prose = (
            "Let me think... the total is 162000. So the answer is:\n"
            f"{json.dumps(VALID_PAYLOAD)}\n\nThat matches the document."
        )
        response = httpx.Response(200, json={
            "choices": [{"message": {"content": prose}}]
        })

        def handler(request: httpx.Request) -> httpx.Response:
            return response

        extractor = OpenAICompatibleInvoiceExtractor(
            api_key="k", model="m", transport=httpx.MockTransport(handler),
        )
        data = extractor.extract("texto")
        assert data.number == "FE364"
        assert int(data.total) == 162000
