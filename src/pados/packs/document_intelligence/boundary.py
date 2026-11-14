# SPDX-License-Identifier: AGPL-3.0-only
"""The closed-error boundary every Document Intelligence stage adapter runs behind.

Failures leave the adapter only as ``CapabilityAdapterError(code)`` raised OUTSIDE the exception handler, so the
exception chain is empty and no document, query or engine text can escape. A governance refusal from the scoped
artifact facade (``ArtifactAccessDenied``) is re-raised untouched.
"""
from __future__ import annotations

from collections.abc import Callable

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactAccessDenied, ArtifactIntegrityError
from pados.packs.algorithms.contracts import AlgorithmError
from pados.packs.document_intelligence.local_models import LocalModelError
from pados.packs.document_intelligence.ranking import DocumentIntelligenceError
from pados.packs.ocr.contracts import OcrContractError
from pados.packs.retrieval.lexical import RetrievalError

UNEXPECTED = "DI_UNEXPECTED_ERROR"


def closed(run: Callable[[StageRequest, StageContext], StageOutput], request: StageRequest,
           context: StageContext) -> StageOutput:
    retryable = False
    try:
        return run(request, context)
    except ArtifactAccessDenied:
        raise
    except CapabilityAdapterError:
        raise
    except (DocumentIntelligenceError, LocalModelError) as exc:
        code, retryable = exc.code, exc.retryable
    except RetrievalError as exc:
        code = exc.code
    except AlgorithmError:
        code = "DI_PARAMETERS_INVALID"
    except OcrContractError:
        code = "DI_DOCUMENT_INVALID"
    except ArtifactIntegrityError:
        code = "DI_INPUT_UNREADABLE"
    except Exception:  # noqa: BLE001 - closed-error boundary
        code = UNEXPECTED
    raise CapabilityAdapterError(code, retryable=retryable) from None  # outside the handler: empty chain
