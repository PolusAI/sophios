from datetime import timedelta
from pathlib import Path
import unittest

import graphviz
from hypothesis import given, settings, HealthCheck
import networkx as nx
import pytest

import sophios
import sophios.ast
import sophios.cli
import sophios.plugins
import sophios.utils
from sophios.wic_types import GraphData, GraphReps, Yaml, YamlTree, StepId

from .test_setup import tools_cwl, yml_paths, validator, wic_strategy


@pytest.mark.skip_pypi_ci
class TestFuzzyCompile(unittest.TestCase):

    @pytest.mark.slow
    @given(wic_strategy)
    @settings(max_examples=100,
              suppress_health_check=[HealthCheck.too_slow,
                                     HealthCheck.filter_too_much],
              deadline=timedelta(milliseconds=20000))
    # TODO: Improve schema so we can remove the health checks
    def test_fuzzy_compile(self, yml: Yaml) -> None:
        """Tests that the compiler doesn't crash when given random allegedly valid input.\n
        Note that the full schema has performance limitations, so a random subset of\n
        wic_main_schema is chosen when hypothesis=True, then random values are generated.

        Args:
            yml (Yaml): Yaml input, randomly generated according to a random subset of wic_main_schema
        """
        plugin_ns = 'global'
        yml_path = Path('random_stepid')
        steps_keys = sophios.utils.get_steps_keys(yml.get('steps', []))
        subkeys = sophios.utils.get_subkeys(steps_keys)
        if subkeys:
            # NOTE: Since all filepaths are currently relative w.r.t. --yaml,
            # we need to supply a fake --yaml. Using [0] works because we are
            # using k=1 in wic_main_schema.
            yml_path_stem = Path(subkeys[0]).stem
            yml_path = yml_paths[plugin_ns][yml_path_stem]

        args = sophios.cli.get_args(str(yml_path))

        y_t = YamlTree(StepId('random_stepid', plugin_ns), yml)
        yaml_tree_raw = sophios.ast.read_ast_from_disk(args.homedir, y_t, yml_paths, tools_cwl, validator,
                                                       args.ignore_validation_errors)
        yaml_tree = sophios.ast.merge_yml_trees(yaml_tree_raw, {}, tools_cwl)
        root_yml_dir_abs = yml_path.parent.absolute()
        yaml_tree = sophios.ast.python_script_generate_cwl(yaml_tree, root_yml_dir_abs, tools_cwl)

        graph_gv = graphviz.Digraph(name=f'cluster_{yml_path}')
        graph_gv.attr(newrank='True')
        graph_nx = nx.DiGraph()
        graphdata = GraphData(str(yml_path))
        graph = GraphReps(graph_gv, graph_nx, graphdata)
        try:
            compiler_info = sophios.compiler.compile_workflow(yaml_tree, args, [], [graph], {}, {}, {},
                                                              {}, tools_cwl, True, relative_run_path=True,
                                                              testing=True)
        except Exception as e:
            multi_def_str = 'Error! Multiple definitions of &'
            unbound_lit_var = 'Error! Unbound literal variable ~'
            python_script = 'Error! Cannot load python_script'
            self_reference = 'Error! Cannot self-reference the same step!'
            # Certain constraints are conditionally dependent on values and are
            # not easily encoded in the schema, so catch them here.
            # Moreover, although we check for the existence of input files in
            # stage_input_files, we cannot encode file existence in json schema
            # to check the python_script script: tag before compile time.
            if multi_def_str in str(e) or unbound_lit_var in str(e) or python_script in str(e) or self_reference in str(e):
                pass
            else:
                # import yaml
                # print(yaml.dump(yml))
                raise e


if __name__ == '__main__':
    sophios.plugins.logging_filters()
    unittest.main()
