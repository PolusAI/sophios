cwlVersion: v1.2
class: CommandLineTool
doc: An input bound unquoted, with a prefix and no valueFrom.
requirements:
  ShellCommandRequirement: {}
baseCommand: echo
inputs:
  text: {type: string, inputBinding: {shellQuote: false, position: 1, prefix: echo}}
outputs: {}
