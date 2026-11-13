# cwltool renames its namespaced classes, by prefix or by URI; embedded, nothing does.
cwlVersion: v1.0
class: CommandLineTool
baseCommand: 'true'
hints:
  cwltool:LoadListingRequirement: {loadListing: shallow_listing}
  http://commonwl.org/cwltool#NetworkAccess: {networkAccess: false}
inputs: {}
outputs: {}
