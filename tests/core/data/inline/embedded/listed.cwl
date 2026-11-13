# A packed tool writes its hints as a list of {class} entries; a requirement it lists wins too.
cwlVersion: v1.0
class: CommandLineTool
baseCommand: 'true'
hints: [{class: ResourceRequirement, ramMin: 8}]
requirements: [{class: NetworkAccess, networkAccess: false}]
inputs: {}
outputs: {}
