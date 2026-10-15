"""Join transcript fragments with Chinese/CJK and Western spacing rules."""

from __future__ import annotations


_ASCII_PUNCT = set(".,!?;:%'\"()[]{}-/\\_`~@#$^&*+=|<>")
_OPENING_BRACKETS = set("([{<“‘「『【《〈〔〖（")
_CLOSING_PUNCT = set(")]}>”’」』】》〉〕〗）.,!?;:%，。！？；：、")
_ASCII_QUOTES = set("\"'")

_CJK_TEXT_RANGES = (
    (0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2EBEF), (0x30000, 0x323AF),  # Ideographs, Ext. A-J
    (0xF900, 0xFAFF), (0x2F800, 0x2FA1F),                                       # Compatibility ideographs
    (0x3040, 0x309F), (0x30A0, 0x30FF), (0x31F0, 0x31FF),                       # Kana
    (0x1B000, 0x1B12F), (0x1B130, 0x1B16F), (0x1AFF0, 0x1AFFF),                 # Kana extensions
    (0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F),                       # Hangul
    (0xA960, 0xA97F), (0xD7B0, 0xD7FF),
    (0x3100, 0x312F), (0x31A0, 0x31BF),                                         # Bopomofo
    (0x3190, 0x31EF), (0x3200, 0x32FF), (0x3300, 0x33FF), (0x4DC0, 0x4DFF),     # Kanbun, strokes, enclosed
    (0xFF00, 0xFFEF),                                                           # Fullwidth forms
)
# Fullwidth and CJK punctuation carries its own spacing.
_CJK_PUNCT_RANGES = (
    (0x3000, 0x303F), (0x2010, 0x2027), (0x2030, 0x205E),
    (0xFF01, 0xFF0F), (0xFF1A, 0xFF20), (0xFF3B, 0xFF40), (0xFF5B, 0xFF65), (0xFFE0, 0xFFEE),
)
_CJK_PUNCT_CHARS = set("，。！？；：、“”‘’《》（）【】〔〕…—～·")


def _in_ranges(char: str, ranges) -> bool:
    cp = ord(char)
    return any(low <= cp <= high for low, high in ranges)


def _is_cjk_punct(char: str) -> bool:
    return _in_ranges(char, _CJK_PUNCT_RANGES) or char in _CJK_PUNCT_CHARS


def _is_cjk_text(char: str) -> bool:
    """Hanzi, kana, hangul and other CJK characters that are not punctuation."""
    return _in_ranges(char, _CJK_TEXT_RANGES) and not _is_cjk_punct(char)


def _is_western_char(char: str) -> bool:
    if _is_cjk_text(char) or _is_cjk_punct(char):
        return False
    return char.isalnum() or (char.isascii() and char not in _ASCII_PUNCT and not char.isspace())


def _needs_space_between(left: str, right: str, left_is_closing: bool = True, right_is_opening: bool = True) -> bool:
    """Whether a space belongs between two adjacent characters.

    A space separates a Western word or closing mark (``word``, ``word.``,
    ``"word"``) from a following Western word or opening mark (``next``,
    ``(next``), and separates CJK text from an adjacent Western word. Nothing
    else gets one: not CJK text from CJK text or from punctuation, and not
    anything next to CJK punctuation.
    """
    if not left or not right or _is_cjk_punct(left) or _is_cjk_punct(right):
        return False
    western_left, western_right = _is_western_char(left), _is_western_char(right)
    ends_phrase = western_left or left in _CLOSING_PUNCT or (left in _ASCII_QUOTES and left_is_closing)
    starts_phrase = western_right or right in _OPENING_BRACKETS or (right in _ASCII_QUOTES and right_is_opening)
    if ends_phrase and starts_phrase:
        return True
    return (_is_cjk_text(left) and western_right) or (western_left and _is_cjk_text(right))


def join_display_text(current: str, addition: str) -> str:
    """Join adjacent display blocks following Chinese/CJK and Western typography rules."""
    current = current.rstrip()
    addition = addition.lstrip()
    if not current:
        return addition
    if not addition:
        return current
    left_is_closing = len(current) > 1 or current not in _ASCII_QUOTES
    right_is_opening = len(addition) > 1 or addition not in _ASCII_QUOTES
    if _needs_space_between(current[-1], addition[0], left_is_closing, right_is_opening):
        return current + " " + addition
    return current + addition
