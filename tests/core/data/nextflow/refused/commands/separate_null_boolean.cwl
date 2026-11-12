cwlVersion: v1.2
class: CommandLineTool
doc: A boolean input bound with separate null and no prefix, which cwltool refuses.
baseCommand: echo
inputs:
  v: {type: boolean, inputBinding: {separate: null}}
outputs: {}
