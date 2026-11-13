# What the tool wrote wins: a hint it declares, and a requirement, so nothing of that class is added.
cwlVersion: v1.0
class: CommandLineTool
baseCommand: 'true'
hints: {NetworkAccess: {networkAccess: false}}
requirements: {LoadListingRequirement: {loadListing: no_listing}}
inputs: {}
outputs: {}
