cwlVersion: v1.2
class: CommandLineTool
doc: Echo a message, a tool default unless a step binds it.
baseCommand: echo
inputs:
  message: {type: string, default: tool default, inputBinding: {position: 1}}
outputs: {}
