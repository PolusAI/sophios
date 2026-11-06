# Plugin discovery layouts

Each case lays out `files` (`.cwl` tools or `.wic` workflows, by `kinds`, default `[tool]`) and `links`, and
searches `roots` (default `{global: ['']}`, the layout itself); no root is ever empty, as discovery then prints to
stdout. It expects `found`, the file each stem resolves to by namespace, and `warnings`, each repeated stem's
files in the order found. Both are derived by hand from the rules the `sophios.plugins` docstrings state, and the
one each `why` names; never filled from a run. Tools are compared in order (search paths in config order, then
sorted path order), workflows as a mapping, whose order no docstring states; no case depends on where a repeated
stem sits.
