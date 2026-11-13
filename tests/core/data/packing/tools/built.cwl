# Like extract_pdbbind_refined.cwl: its image is built from a Dockerfile beside it, and it keeps its own version.
cwlVersion: v1.0
class: CommandLineTool
hints:
  DockerRequirement:
    dockerImageId: built_here
    dockerFile:
      $include: Dockerfile_built
baseCommand: echo
inputs: {}
outputs: {}
