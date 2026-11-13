cwlVersion: v1.2
class: CommandLineTool
doc: Print first-second, with no newline, to out.txt.
baseCommand: printf
arguments:
- {position: 0, valueFrom: '%s-%s'}
inputs:
  first:
    type: string
    inputBinding: {position: 1}
  second:
    type: string
    inputBinding: {position: 2}
stdout: out.txt
outputs:
  line:
    type: File
    outputBinding: {glob: out.txt}
