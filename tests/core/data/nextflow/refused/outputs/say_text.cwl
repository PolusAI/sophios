cwlVersion: v1.2
class: CommandLineTool
doc: Print the message, and give the text of what was printed.
baseCommand: echo
inputs:
  message:
    type: string
    inputBinding: {position: 1}
stdout: out.txt
outputs:
  text:
    type: string
    outputBinding: {glob: out.txt, loadContents: true, outputEval: '$(self[0].contents)'}
