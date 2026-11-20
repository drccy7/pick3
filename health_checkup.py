"""건강검진 결과 PDF detection (text layer only, no Gemini).

Terms come from the national general health checkup result form (일반건강검진 결과통보서) and the
judgment words every checkup report uses. A detailed medical bill can list lab names too, so a checkup
needs report words (결과·판정) as well as several measurement names.
"""

from __future__ import annotations

import io
import re

REPORT_WORDS = ("건강검진", "검진결과", "결과통보", "종합판정", "종합소견", "의심질환", "판정", "정상A", "정상B", "질환의심", "유질환")
MEASURES = ("신장", "체중", "허리둘레", "체질량지수", "혈압", "혈색소", "공복혈당", "총콜레스테롤", "HDL", "LDL",
            "중성지방", "크레아티닌", "사구체여과율", "AST", "ALT", "감마지티피", "요단백", "흉부방사선", "흉부촬영", "시력", "청력")


def is_checkup_text(text: str) -> bool:
    t = re.sub(r"\s+", "", text)
    report = sum(w in t for w in REPORT_WORDS)
    measures = sum(m in t for m in MEASURES)
    return report >= 2 and measures >= 5


def is_checkup_pdf(raw: bytes, pages: int = 4) -> bool:
    if raw[:5] != b"%PDF-":
        return False
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            text = " ".join((p.extract_text() or "") for p in pdf.pages[:pages])
    except Exception:
        return False
    return is_checkup_text(text)
