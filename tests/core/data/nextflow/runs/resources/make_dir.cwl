cwlVersion: v1.2
class: CommandLineTool
doc: Make the directory `made`, asking for 2 cores, 64 MiB and two CUDA devices.
$namespaces:
  cwltool: http://commonwl.org/cwltool#
requirements:
  ResourceRequirement:
    coresMin: 2
    ramMin: 64
hints:
- class: cwltool:CUDARequirement
  cudaVersionMin: '11.4'
  cudaDeviceCountMin: 2
  cudaComputeCapability: ['7.0', '8.0']
baseCommand: [mkdir, made]
inputs: {}
outputs:
  result:
    type: Directory
    outputBinding: {glob: made}
