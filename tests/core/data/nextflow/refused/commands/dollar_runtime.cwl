cwlVersion: v1.2
class: CommandLineTool
doc: An argument whose `$` starts an expression that is not an input reference.
baseCommand: echo
arguments: [$(runtime.cores)]
inputs:
  x: string
outputs: {}
