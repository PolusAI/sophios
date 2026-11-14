cwlVersion: v1.2
class: CommandLineTool
doc: Ask for a fractional number of cores, and for GPUs with counts that are not positive whole numbers.
$namespaces:
  cwltool: http://commonwl.org/cwltool#
requirements:
  ResourceRequirement: {coresMax: 2.5}
hints:
- {class: 'cwltool:CUDARequirement', cudaVersionMin: '11.4', cudaComputeCapability: '3.0', cudaDeviceCountMin: 0, cudaDeviceCountMax: -1}
- {class: 'cwltool:CUDARequirement', cudaVersionMin: '11.4', cudaComputeCapability: '3.0', cudaDeviceCountMin: 2, cudaDeviceCountMax: $(inputs.n)}
baseCommand: 'true'
inputs: {}
outputs: {}
