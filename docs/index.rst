Sophios documentation
=====================

Sophios is a high-level way to create, build, and execute Common Workflow
Language (CWL) workflows. It is for people who already have useful command-line
tools and want to turn them into portable, inspectable workflows without making
raw CWL the day-to-day authoring language.

With Sophios, you describe command-line tool contracts, compose those tools into
workflow graphs, compile the graph to CWL, and run the result locally or prepare
it for execution on remote, HPC, or cloud resources. The generated workflow,
generated job inputs, exported ``.wic`` source, and execution artifacts remain
available for inspection.

The practical outcome is straightforward: workflow authors work in Python by
default, execution remains grounded in standard CWL, and teams can inspect the
exact artifacts that a local runner or remote execution service will receive.

There are two authoring modes:

* the Python Workflow API, which is the primary interface for most users and
  application code;
* the ``.wic`` YAML DSL, which is the file-native interface for standalone,
  headless, auditable, and advanced compiler-oriented workflows.

Why Sophios?
------------

Most scientific and data workflows begin as command-line tools plus a shell
script. That is a good starting point, but it becomes hard to maintain when the
workflow needs explicit tool contracts, portable execution, validated inputs,
generated artifacts, reproducible local runs, or execution on external
resources. Sophios gives that work a clear structure:

* a ``CommandLineTool`` captures what one tool expects and produces,
* a ``Step`` places that tool inside a workflow,
* a ``Workflow`` owns the graph and named outputs,
* compilation emits CWL and job inputs you can inspect,
* submission-oriented helpers validate requests before remote execution.

The goal is not to hide the workflow. The goal is to make the workflow easier to
author while keeping the compiled CWL and execution artifacts concrete enough to
understand, debug, and review.

.. toctree::
   :maxdepth: 2
   :caption: Start Here

   overview.md
   installguide.md
   userguide.md

.. toctree::
   :maxdepth: 2
   :caption: Python API

   multistep_runner.md
   tool_builder_sam3.md
   tool_builder_workflow.md
   compute_request_workflow.md
   ichnaea_compact_compute.md
   python_api_reference.rst

.. toctree::
   :maxdepth: 2
   :caption: Advanced YAML and Operations

   advanced.md
   tutorials/tutorials.rst
   validation.md

.. toctree::
   :maxdepth: 2
   :caption: Developers

   dev/installguide.md
   dev/devguide.md
   dev/algorithms.md
   dev/codingstandards.md
   dev/gitetiquette.md
   dev/api.rst

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
