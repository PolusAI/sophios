# cwltool gives a v1.0 file network access and a deep listing (cwltool/update.py, v1_0to1_1); embedded, nothing
# updates it, so they are written out. Its updater lists a single secondaryFiles pattern, which its checker indexes.
cwlVersion: v1.0
class: CommandLineTool
baseCommand: 'true'
hints: {}
inputs:
  name: {type: File, secondaryFiles: {pattern: .fai}}
  other: {type: File, secondaryFiles: [.crai, .csi]}
  plain: string
outputs:
  file: {type: File, secondaryFiles: .bai, outputBinding: {glob: f}}
