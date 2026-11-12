cwlVersion: v1.2
class: CommandLineTool
doc: Make two files and give both.
baseCommand: [touch, a.txt, b.txt]
inputs: {}
outputs:
  results:
    type: File[]
    outputBinding: {glob: '*.txt'}
