cwlVersion: v1.2
class: CommandLineTool
doc: Write a.txt, and give the files that match *.txt.
baseCommand: [touch, a.txt]
inputs: {}
outputs:
  result: {type: 'File[]', outputBinding: {glob: '*.txt'}}
