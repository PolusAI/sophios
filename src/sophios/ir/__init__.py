"""The typed intermediate representation the compiler lowers to.

Its types are in `types`, composed into a `WorkflowGraph`; lowering is in
`lower`, and the phases that consume a graph are their own modules. Each name
is imported from the module that defines it.
"""
