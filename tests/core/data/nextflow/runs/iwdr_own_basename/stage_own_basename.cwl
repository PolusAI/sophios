{cwlVersion: v1.2, class: CommandLineTool, requirements: {InitialWorkDirRequirement: {listing: [$(inputs.source)]}},
 baseCommand: cat, inputs: {source: {type: File, inputBinding: {position: 1}}}, stdout: out.txt, outputs: {result:
 {type: File, outputBinding: {glob: out.txt}}}}
