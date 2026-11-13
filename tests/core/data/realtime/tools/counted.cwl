# Counts the lines of a log in an image built from the Dockerfile beside it.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: [wc, -l]
hints:
  DockerRequirement:
    dockerPull: counted
    dockerFile: {$include: Dockerfile_count}
inputs:
  log: {type: File, inputBinding: {position: 1}}
stdout: count.txt
outputs:
  count: {type: stdout}
