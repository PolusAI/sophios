cwlVersion: v1.2
class: CommandLineTool
doc: An optional input beside an argument the target cannot read; the argument is the one refusal.
baseCommand: echo
arguments: [$(runtime.cores)]
inputs:
  v: {type: 'string?', inputBinding: {}}
outputs: {}
