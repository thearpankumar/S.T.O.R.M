from enum import Enum


class SupportLevel(str, Enum):
    FULL = "✔"
    NONE = "✘"
    PARTIAL = "Partial"
