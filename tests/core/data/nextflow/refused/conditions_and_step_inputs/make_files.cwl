cwlVersion: v1.2
class: CommandLineTool
doc: Make one file for n, and give it alone and as the files it matches.
baseCommand: touch
arguments: [out.txt]
inputs:
  n: int
outputs:
  file: {type: File, outputBinding: {glob: out.txt}}
  files: {type: 'File[]', outputBinding: {glob: '*.txt'}}
