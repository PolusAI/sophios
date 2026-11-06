{cwlVersion: v1.2, class: CommandLineTool, requirements: {InitialWorkDirRequirement: {listing: [{entry: $(inputs.a),
 writable: true}, $(inputs.b)]}}, baseCommand: ls, inputs: {a: {type: Directory}, b: {type: Directory, inputBinding:
 {position: 1}}}, stdout: listing.txt, outputs: {listing: {type: File, outputBinding: {glob: listing.txt}}}}
