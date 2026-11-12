cwlVersion: v1.2
class: CommandLineTool
doc: Unquoted words the target refuses; only a literal valueFrom, with no `$`, under ShellCommandRequirement is one.
requirements:
  ShellCommandRequirement: {}
baseCommand: echo
arguments:
- {position: 1, shellQuote: false}                                     # no valueFrom
- {position: 2, valueFrom: '>>', shellQuote: false, prefix: -x}       # a prefix
- {position: 3, valueFrom: $(inputs.message), shellQuote: false}       # an input's value
- {position: 4, valueFrom: prefix-$(inputs.message)-suffix, shellQuote: false}
- {position: 5, valueFrom: $(inputs.message.basename), shellQuote: false}
- {position: 6, valueFrom: '[$UNSET]', shellQuote: false}              # a `$` the task's shell would expand
- {position: 7, valueFrom: '\$(date)', shellQuote: false}
inputs:
  message: string
  text: {type: string, inputBinding: {position: 8, prefix: echo, shellQuote: false}}
outputs: {}
