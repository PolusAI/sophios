cwlVersion: v1.2
class: CommandLineTool
doc: Print n, and give the text printed.
baseCommand: echo
inputs:
  n: {type: int, inputBinding: {position: 1}}
stdout: out.txt
outputs:
  text: {type: string, outputBinding: {glob: out.txt, loadContents: true, outputEval: '$(self[0].contents)'}}
