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
              deadline=None)
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
            if yml_path_stem in yml_paths.get(plugin_ns, {}):
                yml_path = yml_paths[plugin_ns][yml_path_stem]

        args = sophios.cli.get_args(str(yml_path))

        y_t = YamlTree(StepId('random_stepid', plugin_ns), yml)

        graph_gv = graphviz.Digraph(name=f'cluster_{yml_path}')
        graph_gv.attr(newrank='True')
        graph_nx = nx.DiGraph()
        graphdata = GraphData(str(yml_path))
        graph = GraphReps(graph_gv, graph_nx, graphdata)

        compiler_options, graph_settings, yaml_tag_paths = sophios.cli.get_dicts_for_compilation()

        try:
            yaml_tree_raw = sophios.ast.read_ast_from_disk(args.homedir, y_t, yml_paths, tools_cwl, validator,
                                                           args.ignore_validation_errors)
            yaml_tree = sophios.ast.merge_yml_trees(
                yaml_tree_raw, {}, tools_cwl)
            root_yml_dir_abs = yml_path.parent.absolute()
            yaml_tree = sophios.ast.python_script_generate_cwl(
                yaml_tree, root_yml_dir_abs, tools_cwl)

            sophios.compiler.compile_workflow(yaml_tree, compiler_options, graph_settings,
                                              yaml_tag_paths, [], [graph], {}, {}, {}, {},
                                              tools_cwl, True, relative_run_path=True, testing=True)
        except BaseException as e:
            expected_messages = (
                'Error! Multiple definitions of &',
                'Error! Unbound literal variable ~',
                'Error! Cannot load python_script',
                'Error! Cannot self-reference the same step!',
                'Error! If steps: tag is a List then all its elements should be Dictionaries!',
                'Error! Each step dictionary must contain a non-empty string id: tag.',
                'Error! If steps: tag is a Dictionary then all its keys should be non-empty strings!',
                'Error! If steps: tag is a Dictionary then all its values should be Dictionaries!',
                'Error! The `out` tag should be a list.',
                'Error! There should only be one non-empty string anchor per out: list entry!',
                'Error! Each out: list entry should be a string or a single-key dictionary.',
                'Error! Each out: list entry should resolve to a string output name before workflow compilation.',
                'Error! Provided input ',
                "Error! Neither ",
                'Error! No implementations and/or steps in ',
                'Error! workflows must define at least one step.',
                'Error! $namespaces tag must be a dictionary if present.',
                'Error! $schemas tag must be a list if present.',
                'Error! Subworkflow has no concrete first step.',
            )
            # Certain constraints are conditionally dependent on values and are
            # not easily encoded in the schema, so catch them here.
            # Moreover, although we check for the existence of input files in
            # stage_input_files, we cannot encode file existence in json schema
            # to check the python_script script: tag before compile time.
            if isinstance(e, SystemExit) and e.code == 1:
                pass
            elif any(msg in str(e) for msg in expected_messages):
                pass
            else:
                # import yaml
                # print(yaml.dump(yml))
                raise e


if __name__ == '__main__':
    sophios.plugins.logging_filters()
    unittest.main()
