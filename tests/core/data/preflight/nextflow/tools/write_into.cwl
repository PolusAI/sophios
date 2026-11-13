# Writes into the output-target Directory `outDir`, with two CUDA fields the Nextflow target notes it does not check.
cwlVersion: v1.2
class: CommandLineTool
$namespaces:
  cwltool: http://commonwl.org/cwltool#
baseCommand: touch
requirements:
  InitialWorkDirRequirement:
    listing:
    - entry: $(inputs.outDir)
      writable: true
hints:
  cwltool:CUDARequirement:
    cudaVersionMin: '11.4'
    cudaComputeCapability: '3.0'
    cudaDeviceCountMin: 1
inputs:
  outDir:
    type: Directory
outputs:
  outDir:
    type: Directory
    outputBinding:
      glob: $(inputs.outDir.basename)
arguments:
- $(inputs.outDir.basename)/result.txt
