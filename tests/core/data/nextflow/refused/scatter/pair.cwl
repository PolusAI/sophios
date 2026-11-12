cwlVersion: v1.2
class: CommandLineTool
doc: Print first and second into out.txt.
baseCommand: echo
inputs:
  first: {type: string, inputBinding: {position: 1}}
  second: {type: string, inputBinding: {position: 2}}
stdout: out.txt
outputs:
  out: {type: File, outputBinding: {glob: out.txt}}
