cwlVersion: v1.2
class: CommandLineTool
doc: Give files, and lists of words.
baseCommand: [touch, a.txt]
inputs: {}
outputs:
  files: {type: 'File[]', outputBinding: {glob: '*.txt'}}
  lists: {type: {type: array, items: {type: array, items: string}}, outputBinding: {glob: a.txt}}
