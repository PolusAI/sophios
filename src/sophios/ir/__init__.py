"""The typed intermediate representation the compiler lowers to.

Eight types composed into a `WorkflowGraph`. Nothing here reads a document or
writes CWL: lowering is in `lower`, and the phases that consume a graph are
their own modules.
"""
from .types import (
    Binding,
    DeferredObligation,
    Direction,
    Edge,
    Namespace,
    JobBinding,
    Port,
    PortDeclaration,
    PortId,
    PortType,
    ProcessRun,
    StepId,
    StepEmission,
    StepNode,
    WorkflowPort,
    WorkflowGraph,
)
from .emit import emit, emit_job_inputs
from .resolve import (
    RegistryKey,
    RegistrySnapshot,
    Resolved,
    ResolvedDocument,
    ResolvedPort,
    ResolvedProcess,
    ResolvedStep,
    ToolDefinition,
    WorkflowSource,
    generated_process_id,
    resolve,
)
from .pipeline import (FrontEndResult, front_end, legacy_after_infer,
                       legacy_after_link, legacy_after_lower)
from .link import Linked, link
from .infer import (Inferred, InferencePolicy, Insertion, InsertionCatalog,
                    infer)

__all__ = [
    'Binding',
    'DeferredObligation',
    'Direction',
    'Edge',
    'Namespace',
    'JobBinding',
    'Port',
    'PortDeclaration',
    'PortId',
    'PortType',
    'ProcessRun',
    'StepId',
    'StepEmission',
    'StepNode',
    'WorkflowPort',
    'WorkflowGraph',
    'emit',
    'emit_job_inputs',
    'RegistryKey',
    'RegistrySnapshot',
    'Resolved',
    'ResolvedDocument',
    'ResolvedPort',
    'ResolvedProcess',
    'ResolvedStep',
    'ToolDefinition',
    'WorkflowSource',
    'generated_process_id',
    'resolve',
    'FrontEndResult',
    'front_end',
    'legacy_after_lower',
    'legacy_after_link',
    'Linked',
    'link',
    'legacy_after_infer',
    'Inferred',
    'InferencePolicy',
    'Insertion',
    'InsertionCatalog',
    'infer',
]
