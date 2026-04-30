"""Project-specific exceptions."""


class AgentError(Exception):
    """Base exception for recoverable agent failures."""


class IngestError(AgentError):
    """Raised when an input document cannot be parsed."""

    def __init__(self, message: str, doc_id: str | None = None, page: int | None = None):
        super().__init__(message)
        self.doc_id = doc_id
        self.page = page


class NoClaimsFound(AgentError):
    """Raised when no row-level claims can be extracted."""


class OrchestratorError(AgentError):
    """Raised when the full run cannot continue."""
