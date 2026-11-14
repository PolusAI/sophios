"""What Sophios does with each field of the CWL v1.2 workflow-level classes.

One entry per schema field, read from `cwl_utils.parser.cwl_v1_2.<Class>.attrs`,
so a field the schema has and this table lacks fails the build
(`tests/core/test_support_matrix.py`).

NATIVE: Sophios reads it and acts on it, or writes it.
PASSTHROUGH: copied out unchanged.
REJECTED: reported with a diagnostic, never silently dropped or coerced. It is positioned at the
value; a step's `run` is positioned at the step, and a workflow output at the document.
"""
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping


class Support(StrEnum):
    """How Sophios treats one field of a CWL workflow-level class."""

    NATIVE = 'native'
    PASSTHROUGH = 'passthrough'
    REJECTED = 'rejected'


_N, _P, _R = Support.NATIVE, Support.PASSTHROUGH, Support.REJECTED

#: Each field's classification, keyed by the cwl_utils class it is a field of.
SUPPORT_MATRIX: Final[Mapping[str, Mapping[str, Support]]] = MappingProxyType({
    'Workflow': {
        'class': _N,  # written by the compiler
        'cwlVersion': _N,  # written; wic035 for an unrunnable value
        'doc': _P,
        'hints': _P,
        'id': _P,
        'inputs': _N,  # merged into; the compiler wins on a collision
        'intent': _P,
        'label': _P,
        'outputs': _N,  # read: outputSource is resolved; merged into
        'requirements': _N,  # merged into
        'steps': _N,
    },
    'WorkflowStep': {
        'doc': _P,
        'hints': _P,
        'id': _N,
        'in': _N,
        'label': _P,
        'out': _N,
        'requirements': _P,
        'run': _N,  # a registry stem, a path relative to the document, or an inline body
        'scatter': _N,
        'scatterMethod': _N,
        'when': _N,
    },
    'WorkflowStepInput': {
        'id': _N,  # the in: key
        'source': _N,  # !*, a bare workflow-input name, or a list of them inside !cwl {source: [...]}
        'default': _N,  # through !cwl {...}
        'label': _N,  # through !cwl {...}
        'linkMerge': _N,  # through !cwl {...}
        'loadContents': _N,  # through !cwl {...}
        'loadListing': _N,  # through !cwl {...}
        'pickValue': _N,  # through !cwl {...}
        'valueFrom': _N,  # through !cwl {...}
    },
    'WorkflowOutputParameter': {
        'id': _N,
        'type': _N,
        'outputSource': _N,  # one step/port reference; a list is wic038
        'format': _P,
        'doc': _P,
        'label': _P,
        'secondaryFiles': _P,
        'streamable': _P,
        'linkMerge': _R,
        'pickValue': _R,
    },
    'WorkflowInputParameter': {
        'id': _N,
        'type': _N,  # read for the reference judgment
        'format': _N,  # read by inference
        'default': _P,
        'doc': _P,
        'label': _P,
        'inputBinding': _P,
        'loadContents': _P,
        'loadListing': _P,
        'secondaryFiles': _P,
        'streamable': _P,
    },
})

#: The keys that make an untagged mapping in `in:` a step-input record rather
#: than a literal: every WorkflowStepInput field but the id.
STEP_INPUT_RECORD_KEYS: Final = frozenset(SUPPORT_MATRIX['WorkflowStepInput']) - {'id'}
