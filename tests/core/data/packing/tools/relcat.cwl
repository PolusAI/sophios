# Its script by `$include`, its data by a relative default: both beside it. It prints `from-include`, then the data.
cwlVersion: v1.2
class: CommandLineTool
requirements:
  InitialWorkDirRequirement:
    listing:
    - entryname: s.sh
      entry:
        $include: s.sh
baseCommand: [sh, s.sh]
inputs:
  extra:
    type: File
    default: {class: File, location: data.txt}
    inputBinding: {position: 1}
outputs:
  out: {type: stdout}
stdout: out.txt
