cwlVersion: v1.2
class: CommandLineTool
doc: Make each file named; give the one .txt file.
baseCommand: touch
inputs:
  names:
    type: string[]
    inputBinding: {position: 1}
outputs:
  result:
    type: File
    outputBinding: {glob: '*.txt'}
