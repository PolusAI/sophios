#!/usr/bin/env cwl-runner
steps:
  nvt_step_1_grompp:
    in:
      config: nvt_step_1_grompp___config
      input_gro_path: nvt_step_1_grompp___input_gro_path
      input_top_zip_path: nvt_step_1_grompp___input_top_zip_path
    run: biobb/biobb_adapters/cwl/biobb_md/grompp.cwl
    out:
    - output_tpr_path
  nvt_step_2_mdrun:
    run: biobb/biobb_adapters/cwl/biobb_md/mdrun.cwl
    in:
      input_tpr_path: nvt_step_1_grompp/output_tpr_path
    out:
    - output_trr_path
    - output_gro_path
    - output_edr_path
    - output_log_path
    - output_xtc_path
    - output_cpt_path
    - output_dhdl_path
  nvt_step_3_gmx_energy:
    in:
      config: nvt_step_3_gmx_energy___config
      output_xvg_path: nvt_step_3_gmx_energy___output_xvg_path
      input_energy_path: nvt_step_2_mdrun/output_edr_path
    run: biobb/biobb_adapters/cwl/biobb_analysis/gmx_energy.cwl
    out:
    - output_xvg_path
cwlVersion: v1.0
class: Workflow
inputs:
  nvt_step_1_grompp___config: string
  nvt_step_1_grompp___input_gro_path: File
  nvt_step_1_grompp___input_top_zip_path: File
  nvt_step_3_gmx_energy___config: string
  nvt_step_3_gmx_energy___output_xvg_path: string
outputs:
  nvt_step_1_grompp___output_tpr_path:
    type: File
    outputSource: nvt_step_1_grompp/output_tpr_path
  nvt_step_2_mdrun___output_trr_path:
    type: File
    outputSource: nvt_step_2_mdrun/output_trr_path
  nvt_step_2_mdrun___output_gro_path:
    type: File
    outputSource: nvt_step_2_mdrun/output_gro_path
  nvt_step_2_mdrun___output_edr_path:
    type: File
    outputSource: nvt_step_2_mdrun/output_edr_path
  nvt_step_2_mdrun___output_log_path:
    type: File
    outputSource: nvt_step_2_mdrun/output_log_path
  nvt_step_2_mdrun___output_cpt_path:
    type: File
    outputSource: nvt_step_2_mdrun/output_cpt_path
  nvt_step_3_gmx_energy___output_xvg_path:
    type: File
    outputSource: nvt_step_3_gmx_energy/output_xvg_path
