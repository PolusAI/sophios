cwlVersion: v1.2
class: CommandLineTool
doc: Optional inputs a step leaves without a value, each read where its absence would change the command.
requirements:
  ShellCommandRequirement: {}
  EnvVarRequirement:
    envDef: [{envName: A, envValue: $(inputs.exported)}]
baseCommand: echo
arguments:
- {position: 10, valueFrom: '--label=$(inputs.flag)'}     # a flag's value, dereferenced
- {position: 11, valueFrom: $(inputs.message)}            # read outside the input's own binding
- {position: 12, valueFrom: $(inputs.unbound)}            # read by another binding, with none of its own
inputs:
  flag: {type: 'boolean?', inputBinding: {position: 1, prefix: --verbose}}
  message: {type: 'string?', inputBinding: {position: 2, prefix: --message}}
  shell: {type: 'string?', inputBinding: {position: 3, valueFrom: printf optional, shellQuote: false}}
  literal: {type: 'string?', inputBinding: {position: 4, prefix: --literal, valueFrom: literal}}
  unbound: 'string?'
  path: 'File?'
  values: {type: 'string[]?', inputBinding: {position: 5, prefix: --value}}
  exported: 'string?'
outputs: {}
