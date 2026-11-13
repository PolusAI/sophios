# Writes system.pdb itself, and also names it as its empty stdout.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: [sh, -c, 'printf "ATOM 1\n" > "$0"']
inputs: {output_pdb_path: {type: string, default: system.pdb, inputBinding: {position: 1}}}
outputs: {output_pdb_path: {type: File, outputBinding: {glob: $(inputs.output_pdb_path)}}}
stdout: $(inputs.output_pdb_path)
