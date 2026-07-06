import re


_TOKEN_PATTERN = re.compile(r"\w+|[^\s\w]", re.UNICODE)


def estimate_tokens(text: str) -> int:
    """Deterministic local token estimate used until model tokenizers are wired in."""
    if not text:
        return 0
    return len(_TOKEN_PATTERN.findall(text))
