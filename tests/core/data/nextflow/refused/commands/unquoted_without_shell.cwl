cwlVersion: v1.2
class: CommandLineTool
doc: An unquoted word with no ShellCommandRequirement.
baseCommand: echo
arguments:
- {valueFrom: '>>', shellQuote: false}
inputs: {}
outputs: {}
