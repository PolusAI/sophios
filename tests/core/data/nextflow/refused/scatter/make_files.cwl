cwlVersion: v1.2
class: CommandLineTool
doc: Make a.txt, and give the files that match *.txt.
baseCommand: [touch, a.txt]
inputs: {}
outputs:
  files: {type: 'File[]', outputBinding: {glob: '*.txt'}}
