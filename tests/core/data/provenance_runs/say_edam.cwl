# Declares EDAM from edamontology's default branch: a URL Sophios never wrote, with the basename of the one it did.
$namespaces: {edam: https://edamontology.org/}
$schemas: ['https://raw.githubusercontent.com/edamontology/edamontology/main/EDAM_dev.owl']
cwlVersion: v1.2
class: CommandLineTool
baseCommand: echo
stdout: out.txt
inputs: {message: {type: string, inputBinding: {position: 1}}}
outputs: {out: {type: File, outputBinding: {glob: out.txt}}}
