"""Capture's platform-independent stages (5.7). The grab, settle loop and window bounds run on the rig; the redaction
stage of 3.6 and 6.10 is `redaction`."""
from .redaction import (OcrWord, RedactionContext, RedactionError, RedactionList, RedactionRegion, RedactionResult,
                        SelfTest, Sweep, SweepHit, SyntheticOcr, WordOcr, h6_frame, h6_self_test, redact_pair,
                        redaction_version, sample_artifacts, verify)

__all__ = [
    "OcrWord", "RedactionContext", "RedactionError", "RedactionList", "RedactionRegion", "RedactionResult",
    "SelfTest", "Sweep", "SweepHit", "SyntheticOcr", "WordOcr", "h6_frame", "h6_self_test", "redact_pair",
    "redaction_version", "sample_artifacts", "verify",
]
