# Renamed in the list form too.
cwlVersion: v1.0
class: CommandLineTool
baseCommand: 'true'
hints: [{class: 'cwltool:LoadListingRequirement', loadListing: no_listing}]
requirements: [{class: 'http://commonwl.org/cwltool#NetworkAccess', networkAccess: true}]
inputs: {}
outputs: {}
