cwlVersion: v1.2
class: CommandLineTool
doc: A valueFrom that computes, which the target does not; only text and input references are kept.
baseCommand: echo
arguments:
- {prefix: -n, valueFrom: $(inputs.a * 2 + 1)}
- {prefix: -c, valueFrom: $(runtime.cores)}
inputs:
  a: {type: int, inputBinding: {position: 1, valueFrom: $(inputs.a * 2 + 1)}}
  b: {type: float, inputBinding: {position: 2, valueFrom: $(Math.round(inputs.b))}}
  e: {type: int, inputBinding: {position: 5, valueFrom: '${return 1}'}}
outputs: {}
