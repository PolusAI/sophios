Python API Reference
====================

This reference documents the public Python surfaces that are intended for user
workflows, tool authoring, compute request construction, and compute submission.

For guided learning, start with :doc:`userguide`, :doc:`tool_builder_sam3`, and
:doc:`compute_request_workflow`. Use this page when you need signatures and
member-level detail.

sophios.api.python.workflow and sophios.api.python.tool_builder
-----------------------------------------------------------------

Import user-facing workflow and tool-authoring objects from their concrete
modules:

.. code-block:: python

   from sophios.api.python.workflow import Step, Workflow
   from sophios.api.python.tool_builder import CommandLineTool, Input, Output

The supported workflow import path is ``sophios.api.python.workflow``.

The detailed member documentation lives in the concrete modules below.

sophios.api.python.workflow
----------------------------

.. automodule:: sophios.api.python.workflow
   :members: Step, Workflow, CompiledWorkflow, InvalidLinkError, InvalidStepError

sophios.api.python.tool_builder
--------------------------------

.. automodule:: sophios.api.python.tool_builder
   :members:

sophios.compute_request
-----------------------

.. automodule:: sophios.compute_request
   :members:
