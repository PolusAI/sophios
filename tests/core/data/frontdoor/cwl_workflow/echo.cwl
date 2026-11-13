cwlVersion: v1.2
class: CommandLineTool
baseCommand: echo
inputs: {text: {type: string, inputBinding: {position: 1}}}
outputs: {out: stdout}
