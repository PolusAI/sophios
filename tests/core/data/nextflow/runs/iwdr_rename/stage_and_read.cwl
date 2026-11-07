{cwlVersion: v1.2, class: CommandLineTool, requirements: {InitialWorkDirRequirement: {listing: [{entry:
 $(inputs.source), entryname: renamed_by_iwdr.txt}]}}, baseCommand: cat, arguments: [renamed_by_iwdr.txt], inputs:
 {source: {type: File}}, stdout: out.txt, outputs: {result: {type: File, outputBinding: {glob: out.txt}}}}
