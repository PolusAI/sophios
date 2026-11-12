cwlVersion: v1.2
class: CommandLineTool
doc: Give 1 as a long.
baseCommand: [echo, '1']
inputs: {}
stdout: n.txt
outputs:
  n: {type: long, outputBinding: {glob: n.txt, loadContents: true, outputEval: '$(self[0].contents)'}}
