cwlVersion: v1.0

class: CommandLineTool

requirements:
  InlineJavascriptRequirement: {}

baseCommand: "true"

# This CommandLineTool is solely to demonstrate the naming conventions.
# Each output simply returns the corresponding input.
# Since both outputs are of type string, inference (by default) cannot
# distinguish between the two outputs by type. With the naming conventions on
# (the default) the output with the matching name wins; with
# --no_inference_naming_conventions inference chooses uniquename1, the output
# declared first.

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