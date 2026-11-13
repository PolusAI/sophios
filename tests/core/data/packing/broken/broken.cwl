# Its `$include` names nothing.
cwlVersion: v1.2
class: CommandLineTool
requirements:
  InitialWorkDirRequirement:
    listing: [{entryname: s.sh, entry: {$include: missing.sh}}]
baseCommand: [sh, s.sh]
inputs: {}
outputs: {}
