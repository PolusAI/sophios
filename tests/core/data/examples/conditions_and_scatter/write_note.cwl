cwlVersion: v1.2
class: CommandLineTool
doc: Write the text to note.txt; `after` only orders this step behind the one that made it.
baseCommand: echo
inputs:
  text:
    type: string
    inputBinding: {position: 1}
  after: string?
stdout: note.txt
outputs:
  note:
    type: File
    outputBinding: {glob: note.txt}
