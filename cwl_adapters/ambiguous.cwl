cwlVersion: v1.0

class: CommandLineTool

requirements:
  InlineJavascriptRequirement: {}

baseCommand: "true"

# This CommandLineTool is solely to demonstrate the naming conventions.
# Each output simply returns the corresponding input.
# Since both outputs are of type string, inference (by default) cannot
# distinguish between the two outputs, and will always choose uniquename2
# since the outputs are considered ordered and matched from bottom to top.

inputs:
  uniquename1:
    type: string
    format: "someformat"

  uniquename2:
    type: string
    format: "someformat"

outputs:
  uniquename1:
    type: string
    # outputs of type: string cannot have formats
    outputBinding:
      outputEval: $(inputs.uniquename1)

  uniquename2:
    type: string
    # outputs of type: string cannot have formats
    outputBinding:
      outputEval: $(inputs.uniquename2)