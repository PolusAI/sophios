Python API reference
====================

What are the exact signatures of the Python API? Two modules make it up:
``sophios.api.python.workflow`` builds workflows, and
``sophios.api.python.tool_builder`` builds the tools they run. Import from the
concrete modules:

.. code-block:: python

   from sophios.api.python.workflow import Step, StepInput, Workflow
   from sophios.api.python.tool_builder import CommandLineTool, Input, Inputs, Output, Outputs, cwl

The guides explain how the pieces fit: the pages under **Write workflows**
for ``Step`` and ``Workflow``, and :doc:`tool-builder` for ``CommandLineTool``.

sophios.api.python.workflow
---------------------------

.. automodule:: sophios.api.python.workflow
   :members: Step, StepInput, Workflow, CompiledWorkflow, ApiError, InvalidInputValueError, InvalidStepError, InvalidLinkError, InvalidCLTError, WorkflowRunError, SophiosError, SophiosErrorCode

sophios.api.python.tool_builder
-------------------------------

.. automodule:: sophios.api.python.tool_builder
   :members:
