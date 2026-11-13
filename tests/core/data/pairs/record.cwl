# What docs/cookbook/record.py and record.wic compile to: two sources merged, in the order written, into one
# File[] input, each tool as the step runs it, and every step output promoted to a workflow output.
steps:
- id: touch
  in:
    filename:
      source: touch~filename
  run:
    class: CommandLineTool
    requirements:
      DockerRequirement:
        dockerPull: docker.io/library/debian:bookworm-slim
      InlineJavascriptRequirement: {}
    baseCommand: touch
    inputs:
      filename:
        type: string
        inputBinding:
          position: 1
    outputs:
      file:
        type: File
        outputBinding:
          glob: $(inputs.filename)
    hints:
      LoadListingRequirement:
        loadListing: deep_listing
      NetworkAccess:
        networkAccess: true
  out:
  - file
- id: echo
  in:
    message:
      source: echo~message
  run:
    class: CommandLineTool
    baseCommand: echo
    inputs:
      message:
        type: string
        inputBinding:
          position: 1
    outputs:
      stdout:
        type: File
        outputBinding:
          glob: stdout
    stdout: stdout
    hints:
      LoadListingRequirement:
        loadListing: deep_listing
      NetworkAccess:
        networkAccess: true
  out:
  - stdout
- id: concat
  in:
    files:
      source:
      - touch/file
      - echo/stdout
      linkMerge: merge_flattened
  run:
    class: CommandLineTool
    baseCommand: cat
    inputs:
      files:
        type: File[]
        inputBinding:
          position: 1
    outputs:
      joined:
        type: File
        outputBinding:
          glob: joined.txt
    stdout: joined.txt
  out:
  - joined
cwlVersion: v1.2
class: Workflow
inputs:
  touch~filename:
    type: string
  echo~message:
    type: string
outputs:
  touch~file:
    type: File
    outputSource: touch/file
  echo~stdout:
    type: File
    outputSource: echo/stdout
  concat~joined:
    type: File
    outputSource: concat/joined
requirements:
  MultipleInputFeatureRequirement: {}
