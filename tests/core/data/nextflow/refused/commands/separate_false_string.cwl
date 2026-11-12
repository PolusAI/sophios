cwlVersion: v1.2
class: CommandLineTool
doc: A string input bound with separate false and no prefix, which cwltool refuses.
baseCommand: echo
inputs:
  v: {type: string, inputBinding: {separate: false}}
outputs: {}
