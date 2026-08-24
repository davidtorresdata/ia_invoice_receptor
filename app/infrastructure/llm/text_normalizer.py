"""Text normalization applied before rule-based extraction.

OCR output is noisy: mixed case, stray accents, broken punctuation,
inflated spacing and occasionally repeated glyphs from scanner streaks.
Every invoice goes through `normalize_invoice_text()` before
`RulesInvoiceExtractor` sees it, so the regex layer works on a canonical
form:

1. Unicode NFKD + drop combining marks  -> "Electrónica" -> "Electronica"
2. Typographic cleanup                  -> NBSP->space, smart quotes,
                                           em-dashes, ellipsis
3. Uppercase everything                 -> single case for all patterns
4. Per-line whitespace collapse         -> tabs/multiple spaces -> one
                                           space, lines trimmed, blank
                                           lines dropped. LINE STRUCTURE
                                           IS PRESERVED: several layouts
                                           print the label alone on its
                                           line and the value below it.
5. Letter-run collapse                  -> runs of identical consecutive
                                           LETTERS collapse to one
                                           ("FFFECHA"/"FACTURAA" ->
                                           "FECHA"/"FACTURA"), EXCEPT the
                                           legitimate Spanish digraphs
                                           LL RR SS CC ("BARRANQUILLA",
                                           "ACCION" survive).
                                           DIGITS are never collapsed:
                                           real invoice numbers contain
                                           repeated digits (1166547846).
"""

import re
import unicodedata

_PUNCTUATION_TRANSLATION = str.maketrans({
    "\u00a0": " ",  # non-breaking space
    "\u2013": "-", "\u2014": "-", "\u2015": "-",  # dashes
    "\u2018": "'", "\u2019": "'",  # single quotes
    "\u201c": '"', "\u201d": '"',  # double quotes
    "\u2026": "...",  # ellipsis
})

_REPEATED_LETTER_RUN = re.compile(r"([A-Z])\1{1,}")

# True doubles that occur in ordinary Spanish words and must survive the
# collapse (BARRANQUILLA, ACCION, PROCESO...). Foreign-name digraphs such
# as FF/MM/NN/PP are NOT protected on purpose: OCR inflates those letters
# far more often than they legitimately double.
_LEGITIMATE_DIGRAPHS = {"LL", "RR", "SS", "CC"}

_INNER_SPACES = re.compile(r"[ \t]+")


def _collapse_letter_run(match: re.Match[str]) -> str:
    run = match.group(0)
    return run if run in _LEGITIMATE_DIGRAPHS else match.group(1)


def normalize_invoice_text(raw: str) -> str:
    """Canonical form used by the rules extractor (see module docstring)."""
    if not raw:
        return ""

    decomposed = unicodedata.normalize("NFKD", raw)
    text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    text = text.translate(_PUNCTUATION_TRANSLATION)
    text = text.upper()

    lines = [
        cleaned
        for line in text.splitlines()
        if (cleaned := _INNER_SPACES.sub(" ", line).strip())
    ]

    return _REPEATED_LETTER_RUN.sub(_collapse_letter_run, "\n".join(lines))


__all__ = ["normalize_invoice_text"]
