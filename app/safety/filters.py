import re
import unicodedata
from dataclasses import dataclass, field


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def check_red_flags(text: str, keywords: list[str]) -> bool:
    normalized = normalize(text)
    return any(normalize(keyword) in normalized for keyword in keywords if keyword.strip())


def needs_secondary_triage(text: str) -> bool:
    normalized = normalize(text)
    symptom_terms = (
        "болит", "болят", "боль", "болезнен", "ноет", "ноющая", "ноющие",
        "опух", "припух", "отек", "кровит", "кровь", "сыпь", "крапивниц",
        "температур", "озноб", "гной", "дышать", "дыхание", "онем", "травм",
        "сломал зуб", "сломала зуб", "сломался зуб", "воспал",
    )
    return any(term in normalized for term in symptom_terms)


@dataclass
class PIIMask:
    mapping: dict[str, str] = field(default_factory=dict)

    def mask(self, text: str) -> str:
        patterns = [
            r"(?<!\w)\+?\d[\d\s()\-]{8,}\d(?!\w)",
            r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}(?!\w)",
            r"(?<!\w)[А-ЯЁ][а-яё]{1,25}\s+[А-ЯЁ][а-яё]{1,25}(?:\s+[А-ЯЁ][а-яё]{1,25})?(?!\w)",
        ]
        for pattern in patterns:
            for match in reversed(list(re.finditer(pattern, text))):
                value = match.group(0)
                token = next((k for k, v in self.mapping.items() if v == value), None)
                if token is None:
                    token = f"[PII_{len(self.mapping) + 1}]"
                    self.mapping[token] = value
                text = text[:match.start()] + token + text[match.end():]
        return text

    def unmask(self, text: str) -> str:
        for token, value in self.mapping.items():
            text = text.replace(token, value)
        return text
