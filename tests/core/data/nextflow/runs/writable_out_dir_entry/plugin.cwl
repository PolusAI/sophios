{cwlVersion: v1.2, class: CommandLineTool,
    requirements: {InitialWorkDirRequirement: {listing: [{entry: $(inputs.outDir),
 writable: true}]}}, baseCommand: [sh, -c, printf written > "$2/result.txt", plugin], inputs: {outDir: {type: Directory,
 inputBinding: {position: 1, prefix: --outDir}}}, outputs: {outDir: {type: Directory, outputBinding: {glob:
 $(inputs.outDir.basename)}}}}
