"""What Sophios does with each field of the CWL v1.2 workflow-level classes.

One row per schema field, read from `cwl_utils.parser.cwl_v1_2.<Class>.attrs`,
so a field the schema has and this table lacks fails the build
(`tests/core/test_support_matrix.py`). Each row names the test that pins it.

NATIVE: Sophios reads it and acts on it, or writes it.
PASSTHROUGH: copied out unchanged.
REJECTED: reported with a diagnostic, never silently dropped or coerced. It is positioned at the
value; a step's `run` is positioned at the step, and a workflow output at the document.
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

_WRITTEN: Final = 'test_class_is_written_while_inputs_outputs_and_version_are_not'
_TOP_LEVEL: Final = 'test_top_level_passthrough_is_byte_identical'
_STEP: Final = 'test_step_passthrough_is_byte_identical'
_INTERPRETED: Final = 'test_every_declared_key_is_interpreted'
_OUTPUT_MERGE: Final = 'test_link_merge_and_pick_value_on_an_output_are_wic038'
_PROMOTED_INPUT: Final = 'test_a_promoted_input_keeps_the_fields_a_workflow_input_may_state'
_PROMOTED_OUTPUT: Final = 'test_a_promoted_output_keeps_the_fields_a_workflow_output_may_state'
_DOCUMENTED: Final = 'test_a_referenced_input_merges_the_documentation_of_the_argument_it_binds'
_RECORD: Final = 'test_a_cwl_record_emits_the_fields_it_carries'

WORKFLOW: Final[Mapping[str, Row]] = MappingProxyType({
    'class': Row(_N, _WRITTEN, 'written by the compiler'),
    'cwlVersion': Row(_N, _WRITTEN, 'written; wic035 for an unrunnable value'),
    'doc': Row(_P, _TOP_LEVEL),
    'hints': Row(_P, _TOP_LEVEL),
    'id': Row(_P, _TOP_LEVEL),
    'inputs': Row(_N, _WRITTEN, 'merged into; the compiler wins on a collision'),
    'intent': Row(_P, _TOP_LEVEL),
    'label': Row(_P, _TOP_LEVEL),
    'outputs': Row(_N, _WRITTEN, 'read: outputSource is resolved; merged into'),
    'requirements': Row(_N, 'test_user_requirements_are_merged_into_not_copied', 'merged into'),
    'steps': Row(_N, 'test_well_formed_documents_parse'),
})

WORKFLOW_STEP: Final[Mapping[str, Row]] = MappingProxyType({
    'doc': Row(_P, _STEP),
    'hints': Row(_P, _STEP),
    'id': Row(_N, 'test_sequence_and_mapping_steps_agree'),
    'in': Row(_N, 'test_input_values_are_closed'),
    'label': Row(_P, _STEP),
    'out': Row(_N, 'test_the_two_spellings_of_an_edge_definition_agree_on_an_output'),
    'requirements': Row(_P, _STEP),
    'run': Row(_N, 'test_an_inline_run_body_is_registered_and_emitted_as_its_own_tool',
               'a registry stem, a path relative to the document, or an inline body'),
    'scatter': Row(_N, _INTERPRETED),
    'scatterMethod': Row(_N, _INTERPRETED),
    'when': Row(_N, _INTERPRETED),
})

WORKFLOW_STEP_INPUT: Final[Mapping[str, Row]] = MappingProxyType({
    'id': Row(_N, 'test_input_values_are_closed', 'the in: key'),
    'source': Row(_N, 'test_input_values_are_closed',
                  '!*, a bare workflow-input name, or a list of them inside !cwl {source: [...]}'),
    'default': Row(_N, _RECORD, 'through !cwl {...}'),
    'label': Row(_N, _RECORD, 'through !cwl {...}'),
    'linkMerge': Row(_N, _RECORD, 'through !cwl {...}'),
    'loadContents': Row(_N, _RECORD, 'through !cwl {...}'),
    'loadListing': Row(_N, _RECORD, 'through !cwl {...}'),
    'pickValue': Row(_N, _RECORD, 'through !cwl {...}'),
    'valueFrom': Row(_N, _RECORD, 'through !cwl {...}'),
})

WORKFLOW_OUTPUT_PARAMETER: Final[Mapping[str, Row]] = MappingProxyType({
    'id': Row(_N, _WRITTEN),
    'type': Row(_N, 'test_an_untyped_authored_output_takes_its_producers_type'),
    'outputSource': Row(_N, 'test_an_authored_output_source_validates', 'one step/port reference; a list is wic038'),
    'format': Row(_P, _WRITTEN),
    'doc': Row(_P, _WRITTEN),
    'label': Row(_P, _WRITTEN),
    'secondaryFiles': Row(_P, _PROMOTED_OUTPUT),
    'streamable': Row(_P, _PROMOTED_OUTPUT),
    'linkMerge': Row(_R, _OUTPUT_MERGE),
    'pickValue': Row(_R, _OUTPUT_MERGE),
})

WORKFLOW_INPUT_PARAMETER: Final[Mapping[str, Row]] = MappingProxyType({
    'id': Row(_N, _WRITTEN),
    'type': Row(_N, 'test_only_proven_disjoint_cross_scope_types_are_rejected', 'read for the reference judgment'),
    'format': Row(_N, 'test_promoted_input_preserves_a_cwl_format_expression', 'read by inference'),
    'default': Row(_P, _WRITTEN),
    'doc': Row(_P, _DOCUMENTED),
    'label': Row(_P, _DOCUMENTED),
    'inputBinding': Row(_P, _WRITTEN),
    'loadContents': Row(_P, _PROMOTED_INPUT),
    'loadListing': Row(_P, _PROMOTED_INPUT),
    'secondaryFiles': Row(_P, _PROMOTED_INPUT),
    'streamable': Row(_P, _PROMOTED_INPUT),
})

#: Every table, keyed by the cwl_utils class it classifies.
SUPPORT_MATRIX: Final[Mapping[str, Mapping[str, Row]]] = MappingProxyType({
    'Workflow': WORKFLOW,
    'WorkflowStep': WORKFLOW_STEP,
    'WorkflowStepInput': WORKFLOW_STEP_INPUT,
    'WorkflowOutputParameter': WORKFLOW_OUTPUT_PARAMETER,
    'WorkflowInputParameter': WORKFLOW_INPUT_PARAMETER,
})

#: The keys that make an untagged mapping in `in:` a step-input record rather
#: than a literal: every WorkflowStepInput field but the id.
STEP_INPUT_RECORD_KEYS: Final = frozenset(WORKFLOW_STEP_INPUT) - {'id'}
