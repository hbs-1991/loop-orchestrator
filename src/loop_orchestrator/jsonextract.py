"""Robust extraction of the final JSON object from an agent message.

Agents are asked to end with a bare JSON object, but in practice they often
precede it with prose or code fences that contain braces of their own (seen
live: the planning advisor wrote "`{op: ...}`" in its analysis before the
verdict). A greedy `\\{.*\\}` regex spans from the first prose brace to the
end and yields invalid JSON — so instead every '{' is tried as a JSON
candidate and the last valid object wins, preferring objects that carry the
key the caller expects.
"""
import json


def find_json_object(text: str, prefer_key: str | None = None) -> dict | None:
    decoder = json.JSONDecoder()
    text = text or ""
    last: dict | None = None
    preferred: dict | None = None
    pos = 0
    while (start := text.find("{", pos)) != -1:
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(obj, dict):
            last = obj
            if prefer_key is not None and prefer_key in obj:
                preferred = obj
            pos = end
        else:
            pos = start + 1
    return preferred if preferred is not None else last
