cwlVersion: v1.2
class: CommandLineTool
doc: Sort src into sorted.txt, which the command names and the glob collects.
baseCommand: sort
arguments: [-o, sorted.txt]
inputs:
  src: {type: File, inputBinding: {position: 1}}
outputs:
  out: {type: File, outputBinding: {glob: sorted.txt}}
