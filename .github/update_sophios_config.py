import json
from pathlib import Path

config_file = Path(__file__).parent.parent / 'src' / 'sophios' / 'config_basic.json'
workdir_path = Path(__file__).parent.parent.parent
config = {}
# config_file can contain absolute or relative paths
with open(config_file, 'r', encoding='utf-8') as f:
    config = json.load(f)

conf_tags = ['search_paths_cwl', 'search_paths_wic']

cwl_locations = [            
                "image-workflows/cwl_adapters",
                "biobb_adapters/biobb_adapters",
                "mm-workflows/cwl_adapters",
                "sophios/cwl_adapters"
                ]

wic_locations = [
                "sophios/docs/tutorials",
                "image-workflows/workflows",
                "mm-workflows/examples"
                ]

gpu_cwl_locations = [
                    "mm-workflows/gpu"
                    ]


updated_cwl_locations = []
for loc in cwl_locations:
    updated_cwl_locations.append(str(workdir_path / loc))
config['search_paths_cwl']['global'] = updated_cwl_locations

updated_gpu_cwl_locations = []
for loc in gpu_cwl_locations:
    updated_gpu_cwl_locations.append(str(workdir_path / loc))
config['search_paths_cwl']['gpu'] = updated_gpu_cwl_locations


updated_wic_loactions = []
for loc in wic_locations:
    updated_wic_loactions.append(str(workdir_path / loc))
config['search_paths_wic']['global'] = updated_wic_loactions

config_path = Path.home() / 'wic'
config_path.mkdir(parents=True, exist_ok=True)
# global config file in ~/wic
global_config_file = config_path / 'global_config.json'
with global_config_file.open('w', encoding='utf-8') as f:
    json.dump(config, f, indent=4)
