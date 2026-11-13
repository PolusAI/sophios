# Its own success codes, kept beside the policy's; and an output in each form CWL writes a type in: one string, an
# array or a record in full, a union with and without null.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
successCodes: [3, 0]
inputs: {}
outputs:
  listed: {type: 'File[]', outputBinding: {glob: '*.txt'}}
  files: {type: {type: array, items: File}, outputBinding: {glob: '*.txt'}}
  either: {type: [File, Directory], outputBinding: {glob: out}}
  maybe: {type: ['null', File], outputBinding: {glob: out}}
  pair: {type: {type: record, fields: {left: {type: File, outputBinding: {glob: left}}}}}
  made: {type: File, outputBinding: {glob: made.txt}}
