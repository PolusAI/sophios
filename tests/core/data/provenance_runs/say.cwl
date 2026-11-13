cwlVersion: v1.2
class: CommandLineTool
baseCommand: echo
stdout: out.txt
inputs: {message: {type: string, inputBinding: {position: 1}}}
outputs: {out: {type: File, outputBinding: {glob: out.txt}}}
