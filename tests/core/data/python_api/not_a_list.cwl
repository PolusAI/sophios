cwlVersion: v1.2
class: CommandLineTool
doc: A listing that is not a list, which cwl_utils reads and cwltool's validator refuses.
requirements:
  InitialWorkDirRequirement: {listing: not-a-list}
baseCommand: 'true'
inputs: {}
outputs: {}
