cwlVersion: v1.2
class: CommandLineTool
doc: An argument whose position is true, which Python, not CWL, reads as 1.
baseCommand: echo
arguments: [{valueFrom: x, position: true}]
inputs: {}
outputs: {}
