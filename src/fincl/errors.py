class FinCLError(Exception):
    """Base class for expected, user-facing FinCL failures."""


class EvidenceError(FinCLError):
    """Evidence could not be captured or verified safely."""


class ExtractionError(FinCLError):
    """Extraction could not be completed."""


class SchemaVersionError(FinCLError):
    """The on-disk database schema is incompatible with this build."""
