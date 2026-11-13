cwlVersion: v1.2
class: CommandLineTool
baseCommand: echo
inputs: {message: {type: string, default: hi, inputBinding: {position: 1}}}
outputs: {out: {type: stdout}}
stdout: out.txt
