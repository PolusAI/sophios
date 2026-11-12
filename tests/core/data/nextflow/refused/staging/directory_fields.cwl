cwlVersion: v1.2
class: CommandLineTool
doc: A Directory whose listing is loaded, and a File with secondary files; each is staged whole and alone.
baseCommand: ls
inputs:
  d: {type: Directory, loadListing: deep_listing, inputBinding: {position: 1}}
  f: {type: File, secondaryFiles: [.bai], inputBinding: {position: 2}}
outputs: {}
