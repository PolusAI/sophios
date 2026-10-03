"""What Sophios does with each field of the CWL v1.2 workflow-level classes.

One row per schema field, read from `cwl_utils.parser.cwl_v1_2.<Class>.attrs`,
so a field the schema has and this table lacks fails the build
(`tests/core/test_support_matrix.py`). Each row names the test that pins it.

NATIVE: Sophios reads it and acts on it, or writes it.
PASSTHROUGH: copied out unchanged.
REJECTED: reported with a positioned diagnostic, never silently dropped or coerced.
"""
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping


class Support(StrEnum):
    """How Sophios treats one field of a CWL workflow-level class."""

    NATIVE = 'native'
    PASSTHROUGH = 'passthrough'
    REJECTED = 'rejected'


@dataclass(frozen=True, slots=True)
class Row:
    """One field's classification and the test function that pins it."""

    support: Support
    pinned_by: str
    note: str = ''


_N, _P, _R = Support.NATIVE, Support.PASSTHROUGH, Support.REJECTED

WORKFLOW_STEP_INPUT: Final[Mapping[str, Row]] = MappingProxyType({
    'id': Row(_N, 'test_input_values_are_closed', 'the in: key'),
    'source': Row(_N, 'test_input_values_are_closed', 'spelled !* or a bare workflow-input name'),
    'default': Row(_R, 'test_an_untagged_step_input_record_is_wic038'),
    'label': Row(_R, 'test_an_untagged_step_input_record_is_wic038'),
    'linkMerge': Row(_R, 'test_an_untagged_step_input_record_is_wic038'),
    'loadContents': Row(_R, 'test_an_untagged_step_input_record_is_wic038'),
    'loadListing': Row(_R, 'test_an_untagged_step_input_record_is_wic038'),
    'pickValue': Row(_R, 'test_an_untagged_step_input_record_is_wic038'),
    'valueFrom': Row(_R, 'test_an_untagged_step_input_record_is_wic038'),
})

#: The keys that make an untagged mapping in `in:` a step-input record rather
#: than a literal: every WorkflowStepInput field but the id.
STEP_INPUT_RECORD_KEYS: Final = frozenset(WORKFLOW_STEP_INPUT) - {'id'}
