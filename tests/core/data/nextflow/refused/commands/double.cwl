cwlVersion: v1.2
class: ExpressionTool
doc: Double x; a process runs a CommandLineTool.
requirements:
  InlineJavascriptRequirement: {}
inputs:
  x: int
outputs:
  y: int
expression: '$({y: inputs.x * 2})'
