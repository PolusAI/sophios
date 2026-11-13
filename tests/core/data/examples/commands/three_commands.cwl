cwlVersion: v1.2
class: CommandLineTool
doc: Run three echo commands in one shell command line; out.txt takes the output of the first two, err.txt the third's.
requirements:
  ShellCommandRequirement: {}
baseCommand: echo
arguments:
- {position: 1, valueFrom: first}
- {position: 2, valueFrom: ;, shellQuote: false}
- {position: 3, valueFrom: echo}
- {position: 4, valueFrom: second}
- {position: 5, valueFrom: ;, shellQuote: false}
- {position: 6, valueFrom: echo}
- {position: 7, valueFrom: third}
- {position: 8, valueFrom: '>&2', shellQuote: false}
- {position: 9, valueFrom: ;, shellQuote: false}
- {position: 10, valueFrom: 'true'}
inputs:
  flag: {type: boolean, inputBinding: {position: 11, valueFrom: --literal, shellQuote: false}}   # text, not a flag
stdout: out.txt
stderr: err.txt
outputs:
  result:
    type: File
    outputBinding: {glob: out.txt}
  errors:
    type: File
    outputBinding: {glob: err.txt}
