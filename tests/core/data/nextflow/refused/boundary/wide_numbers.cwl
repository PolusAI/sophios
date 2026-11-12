cwlVersion: v1.2
class: CommandLineTool
doc: A long and a double as inputs, as array items and as outputs; one long has a default.
baseCommand: echo
inputs:
  a_long: {type: long, inputBinding: {position: 1}}
  a_double: {type: double, inputBinding: {position: 2}}
  longs: {type: {type: array, items: long}, inputBinding: {position: 3}}
  doubles: {type: {type: array, items: double}, inputBinding: {position: 4}}
  defaulted: {type: long, default: 1}
outputs:
  long_out: {type: long, outputBinding: {glob: out.txt}}
  double_out: {type: double, outputBinding: {glob: out.txt}}
