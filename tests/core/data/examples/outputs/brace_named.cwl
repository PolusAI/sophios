cwlVersion: v1.2
class: CommandLineTool
doc: Write a file whose name holds braces, and ab.txt, which a brace expansion would match instead.
baseCommand: [sh, -c, "echo literal > 'a{b,c}.txt'; echo decoy > ab.txt"]
inputs: {}
outputs:
  result:
    type: File
    outputBinding: {glob: 'a{b,c}.txt'}
