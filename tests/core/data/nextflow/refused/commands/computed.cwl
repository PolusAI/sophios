cwlVersion: v1.2
class: CommandLineTool
doc: A valueFrom that computes, which the target does not; only text and input references are kept.
baseCommand: echo
arguments:
- {prefix: -n, valueFrom: $(inputs.a * 2 + 1)}
- {prefix: -m, valueFrom: $(inputs.a > 1)}
- {prefix: -r, valueFrom: $(Math.round(inputs.b))}
- {prefix: -c, valueFrom: $(runtime.cores)}
- {prefix: -j, valueFrom: '${return 1}'}
inputs:
  a: {type: int, inputBinding: {position: 1, valueFrom: $(inputs.a * 2 + 1)}}
  b: {type: float, inputBinding: {position: 2, valueFrom: $(Math.round(inputs.b))}}
  c: {type: int, inputBinding: {position: 3, valueFrom: $(inputs.c > 1)}}
  d: {type: int, inputBinding: {position: 4, valueFrom: $(runtime.cores)}}
  e: {type: int, inputBinding: {position: 5, valueFrom: '${return 1}'}}
  f: {type: int, inputBinding: {position: 6, valueFrom: $(Math.round(inputs.b))}}   # of another input
outputs: {}
