cwlVersion: v1.2
class: CommandLineTool
doc: Sort src into sorted.txt.
baseCommand: sort
stdout: sorted.txt
inputs:
  src: {type: File, inputBinding: {position: 1}}
outputs:
  out: {type: File, outputBinding: {glob: sorted.txt}}
