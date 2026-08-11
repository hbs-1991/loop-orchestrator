"""The span record and its identifiers.

Deliberately not the OpenTelemetry SDK. These spans are reconstructed from a file
after the work has finished, with timestamps, trace ids and parent ids we compute
ourselves; the SDK is built around ambient context in a live process. See the
"hand-rolled OTLP emitter" decision in the spec.
"""
import hashlib
import secrets as _secrets
from dataclasses import dataclass, field

OK = "ok"
ERROR = "error"


def new_span_id() -> str:
    """16 hex chars — an 8-byte span id, per the OTLP wire format."""
    return _secrets.token_hex(8)


def new_trace_id() -> str:
    """32 hex chars — a 16-byte trace id."""
    return _secrets.token_hex(16)


def trace_id_for_run(run_id: int) -> str:
    """A stable trace id derived from the run id.

    Stable on purpose. A Run outlives the process: the orchestrator restarts and
    recovers it, a human revises it hours after the pause and every stage runs
    again. A random id would scatter one Run across several traces, which is
    exactly the view we are building this to avoid.
    """
    return hashlib.sha256(f"loop-run-{run_id}".encode()).hexdigest()[:32]


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str = field(default_factory=new_span_id)
    parent_id: str | None = None
    start_ns: int = 0
    end_ns: int = 0
    attributes: dict = field(default_factory=dict)
    status: str = OK
    error_message: str = ""

    @property
    def duration_ms(self) -> float:
        return max(0, self.end_ns - self.start_ns) / 1e6

    def set(self, **attrs) -> "Span":
        """Attach attributes, dropping the ones with no value.

        `None` means "we did not learn this", and an attribute whose value is
        None is worse than an absent one: it reads as a measurement that came
        back empty.
        """
        for k, v in attrs.items():
            if v is not None and v != "":
                self.attributes[k] = v
        return self

    def fail(self, message: str) -> "Span":
        self.status = ERROR
        self.error_message = message
        return self
