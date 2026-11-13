# Names its own stdout.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: echo
stdout: own.txt
inputs: {}
outputs: {out: {type: File, outputBinding: {glob: own.txt}}}
