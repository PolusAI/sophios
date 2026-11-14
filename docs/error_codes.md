# Error codes

What does a code in a Sophios message mean, and what do I change? This page is
the catalog, one row per code; `sophios --explain <code>` prints the same row.

Every problem Sophios reports carries a code: `wic0NN` for a document or for
what running it needs from this machine, `api0NN` for a Python API call, and
`nf-sNNN` for what the Nextflow target cannot run yet. A
diagnostic reads `file:line:col: error [code] message (step, port)`; the
position and the step are there when Sophios knows them. A note reports a
choice Sophios made and stops nothing.

The kind says what to change:

- `document`: the `.wic` file, the Python workflow, a tool's CWL file, or an imported Nextflow pipeline;
- `call`: the Python API call;
- `machine`: this machine, what is installed, running, readable or writable;
- `run`: nothing in Sophios, a tool failed while it ran;
- `target`: nothing in the workflow; the target cannot run it yet. Change the
  construct the message names, or compile with `--target cwl`.

An `nf-sNNN` code is the Nextflow target's support table: when the target
learns a construct, its code is retired and never reused.

`wic040`, `wic041` and `wic042` are not assigned. Section numbers (§) refer to the
[`.wic` reference](wic-reference.md). [Checks and errors](checks-and-errors.md)
explains when each kind of check runs and how a diagnostic is laid out. In a
checkout the rows are in `src/sophios/lang/error_codes.py`; the site builds the
table from it.

<!-- error-code-table -->

## Pinning an inference choice

A `wic043` note lists the edges inference guessed, one row each, as
`sink <- source`, and `wic055` says which inputs would read one output. To
settle one, write the edge yourself: mark the output with `!&` and read it on
the input with `!*`. For `word_count.text <- shout.shouted` that is
`out: - shouted: !& text` on `shout` and `in: text: !* text` on
`word_count`. For `wic055`, write the edge each of the named inputs should
read. An output inside a subworkflow is pinned on the inner step its path
names, as `sub.wic/inner.file`, and an input that a row names at a
subworkflow, as `sub.wic.file`, on the inner step that declares that port. A
written edge is not a guess, so its row goes away.
[Edge inference](edge-inference.md) walks through each case.
