cwlVersion: v1.2
class: CommandLineTool
doc: Count the words of a text, as a one-line summary and a line-numbered copy.
requirements:
  DockerRequirement:
    dockerPull: debian:bookworm-slim
baseCommand: [sh, -c]
arguments:
- wc -w < $(inputs.text.path) > summary.txt; cat -n $(inputs.text.path) > details.txt
inputs:
  text:
    type: File
outputs:
  output_details:
    type: File
    outputBinding:
      glob: details.txt
  output_summary:
    type: File
    outputBinding:
      glob: summary.txt
