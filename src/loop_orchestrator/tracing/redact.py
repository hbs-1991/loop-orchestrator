"""Truncated previews with the project's secrets taken out.

The spec caps exported content at a preview per field. That alone is not enough:
the values in `secrets/<owner>__<repo>.env` never enter a prompt, but an agent
that runs `env | grep API` or a test that echoes its configuration puts one into
a tool result, and the tool result is what we are about to preview.
"""
from collections.abc import Iterable

MASK = "***"
# Below this length a "secret" is a substring of ordinary text. Redacting a value
# of "1" would blot out every digit in every preview and tell us nothing.
MIN_SECRET_LEN = 4


class Redactor:
    def __init__(self, secret_values: Iterable[str] = ()):
        # Longest first: when one secret contains another, masking the longer one
        # first stops the shorter one from cutting it in half and leaving a tail
        # of the longer value in the output.
        self._secrets = sorted(
            {s for s in secret_values if s and len(s) >= MIN_SECRET_LEN},
            key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for s in self._secrets:
            if s in text:
                text = text.replace(s, MASK)
        return text

    def preview(self, text, limit: int) -> str:
        """Collapse whitespace, remove secrets, then truncate.

        The order matters. Truncating first can cut a secret in half and leave
        the first half in the span — visibly a fragment, still a fragment of a
        credential.
        """
        if text is None:
            return ""
        collapsed = " ".join(str(text).split())
        if not collapsed:
            return ""
        cleaned = self.scrub(collapsed)
        if limit <= 0 or len(cleaned) <= limit:
            return cleaned
        return cleaned[:limit] + "..."
