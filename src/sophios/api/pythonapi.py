# pylint: disable=W1203
"""CLT utilities."""
import logging
from pathlib import Path
from typing import Any, ClassVar, Optional, TypeVar, Union

import cwl_utils.parser as cu_parser
import yaml
from mergedeep import merge, Strategy
from cwl_utils.parser import CommandLineTool as CWLCommandLineTool
from cwl_utils.parser import load_document_by_uri, load_document_by_yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from sophios import compiler, input_output, plugins, utils_cwl, post_compile
from sophios import run_local as run_local_module
from sophios.cli import get_args
from sophios.utils_graphs import get_graph_reps
from sophios.wic_types import CompilerInfo, RoseTree, StepId, Tool, Tools, YamlTree, Json, Cwl, NodeData

from ._types import ScatterMethod


global_config: Tools = {}


logger = logging.getLogger("WIC Python API")


class DisableEverythingFilter(logging.Filter):
    # pylint:disable=too-few-public-methods
    def filter(self, record: logging.LogRecord) -> bool:
        return False


# Based on user feedback,
# disable any and all warnings coming from autodiscovery.
logger_wicad = logging.getLogger("wicautodiscovery")
logger_wicad.addFilter(DisableEverythingFilter())


_WIC_PATH = Path(__file__).parent.parent.parent.parent  # WIC dir


class InvalidInputValueError(Exception):
    pass


class MissingRequiredValueError(Exception):
    pass


class InvalidStepError(Exception):
    pass


class InvalidLinkError(Exception):
    pass


class InvalidCLTError(ValueError):
    pass


CWLInputParameter = Union[
    cu_parser.cwl_v1_0.CommandInputParameter,
    cu_parser.cwl_v1_1.CommandInputParameter,
    cu_parser.cwl_v1_2.CommandInputParameter,
]  # cwl_utils does not have CommandInputParameter union

CWLOutputParameter = Union[
    cu_parser.cwl_v1_0.CommandOutputParameter,
    cu_parser.cwl_v1_1.CommandOutputParameter,
    cu_parser.cwl_v1_2.CommandOutputParameter,
]  # cwl_utils does not have CommandInputParameter union

StrPath = TypeVar("StrPath", str, Path)


class ProcessInput(BaseModel):  # pylint: disable=too-few-public-methods
    """Input of CWL CommandLineTool or Workflow."""

    inp_type: Any
    name: str
    # NOTE: Not optional, but we can't initialize it yet
    parent_obj: Any = Field(default=None)  # Process: Union[Step, Workflow]
    value: Any = Field(default=None)  # validation happens at assignment
    required: bool = True
    linked: bool = False

    def __init__(self, name: str, inp_type: Any) -> None:
        inp_type = utils_cwl.canonicalize_type(inp_type)
        if isinstance(inp_type, list) and "null" in inp_type:
            required = False
        else:
            required = True
        super().__init__(inp_type=inp_type, name=name, required=required)

    @field_validator("name", mode="before")
    @classmethod
    def get_name_from_id(cls, cwl_id: str) -> Any:
        """Return name of input from InputParameter.id."""
        return cwl_id.split("#")[-1]

    @field_validator("inp_type", mode="before")
    @classmethod
    def set_inp_type(cls, inp: Any) -> Any:
        """Return inp_type."""
        if isinstance(inp, list):  # optional inps
            inp = inp[1]
        return inp

    def _set_value(
        self, __value: Any, linked: bool = False
    ) -> None:
        """Set input value."""
        self.value = __value  # to be used for linking inputs */&
        if linked:
            self.linked = True


class ProcessOutput(BaseModel):  # pylint: disable=too-few-public-methods
    """Output of CWL CommandLineTool or Workflow."""

    out_type: Any
    name: str
    # NOTE: Not optional, but we can't initialize it yet
    parent_obj: Any = Field(default=None)  # Process: Union[Step, Workflow]
    value: Any = Field(default=None)  # validation happens at assignment
    required: bool = True
    linked: bool = False

    def __init__(self, name: str, out_type: Any) -> None:
        out_type = utils_cwl.canonicalize_type(out_type)
        if isinstance(out_type, list) and "null" in out_type:
            required = False
        else:
            required = True
        super().__init__(out_type=out_type, name=name, required=required)

    @field_validator("name", mode="before")
    @classmethod
    def get_name_from_id(cls, cwl_id: str) -> Any:
        """Return name of input from InputParameter.id."""
        return cwl_id.split("#")[-1]

    @field_validator("out_type", mode="before")
    @classmethod
    def set_out_type(cls, out: Any) -> Any:
        """Return out_type."""
        if isinstance(out, list):  # optional outs
            out = out[1]
        return out

    def _set_value(
        self, __value: Any, linked: bool = False
    ) -> None:
        """Set output value."""
        self.value = __value  # to be used for linking inputs */&
        if linked:
            self.linked = True


def _default_dict() -> dict:
    return {}


def _get_value_from_cfg(value: Any) -> Any:  # validation happens in Step
    if isinstance(value, dict):
        if "Directory" in value.values():
            try:
                value_ = Path(value["location"])
            except BaseException as exc:
                raise InvalidInputValueError() from exc
            if not value_.is_dir():
                raise InvalidInputValueError(f"{str(value_)} is not a directory")
            return value_
        return value
    return value


# Process = Union[Step, Workflow]


def set_input_Step_Workflow(process_self: Any, __name: str, __value: Any) -> Any:
    index = process_self._input_names.index(__name)
    if isinstance(__value, ProcessOutput):
        process_other = __value.parent_obj
        if not __value.linked:
            try:
                local_input = process_self.inputs[index]
                # NOTE: Relax exact equality for Any type
                # if not local_input.inp_type == __value.out_type and not local_input.inp_type == Any and not __value.out_type == Any:
                #     raise InvalidLinkError(
                #         f"links must have the same input type. "
                #         f"cannot link {local_input.name} to {__value.name}"
                #         f"with types {local_input.inp_type} to {__value.out_type}"
                #     )
                if isinstance(process_other, Workflow):
                    tmp = __value.name  # Use the formal parameter / variable name
                    local_input._set_value(f"{tmp}", linked=True)
                    __value._set_value(f"{tmp}", linked=True)
                else:
                    # Use the current value so we can exactly reproduce hand-crafted wic files.
                    # (Very useful for regression testing!)
                    # NOTE: process_name is either clt name or workflow name
                    tmp = __value.value if __value.value else f"{__name}{process_self.process_name}"
                    alias_dict = {'wic_alias': tmp}
                    local_input._set_value(alias_dict, linked=True)
                    anchor_dict = {'wic_anchor': tmp}
                    __value._set_value(anchor_dict, linked=True)
            except BaseException as exc:
                raise exc
        else:  # value is already linked to another inp
            try:
                local_input = process_self.inputs[index]
                # NOTE: Relax exact equality for Any type
                # if not local_input.inp_type == __value.out_type and not local_input.inp_type == Any and not __value.out_type == Any:
                #     raise InvalidLinkError(
                #         f"links must have the same input type. "
                #         f"cannot link {local_input.name} to {__value.name} "
                #         f"with types {local_input.inp_type} to {__value.out_type}"
                #     )
                if isinstance(process_other, Workflow):
                    tmp = __value.name  # Use the formal parameter / variable name
                    local_input._set_value(f"{tmp}", linked=True)
                    __value._set_value(f"{tmp}", linked=True)
                else:
                    anchor_dict = __value.value
                    alias_dict = {'wic_alias': anchor_dict['wic_anchor']}
                    local_input._set_value(alias_dict, linked=True)
            except BaseException as exc:
                raise exc

    else:
        # obj = process_self.inputs[index]
        # NOTE: "TypeError: typing.Any cannot be used with isinstance()"
        # if not obj.inp_type == Any and not isinstance(__value, obj.inp_type) and not isinstance(__value, list):
        #     raise TypeError(
        #         f"invalid attribute type for {obj.name}: "
        #         f"got {__value.__class__.__name__}, "
        #         f"expected {obj.inp_type.__name__}"
        #     )
        ii_dict = {'wic_inline_input': __value}
        process_self.inputs[index]._set_value(ii_dict)


class Step(BaseModel):  # pylint: disable=too-few-public-methods
    """Base class for Step of Workflow."""
    model_config: ClassVar[ConfigDict] = ConfigDict(arbitrary_types_allowed=True)

    clt: CWLCommandLineTool
    clt_path: Path
    process_name: str
    cwl_version: str
    inputs: list[ProcessInput]
    outputs: list[ProcessOutput]
    yaml: dict[str, Any]
    cfg_yaml: dict = Field(default_factory=_default_dict)

    # these are not part of 'clt data'
    scatter: list[ProcessInput] = []
    scatterMethod: str = ''
    # use when tag to enable conditional steps
    when: str = ''
    _input_names: list[str] = PrivateAttr(default_factory=list)
    _output_names: list[str] = PrivateAttr(default_factory=list)

    def __init__(self, clt_path: StrPath, config_path: Optional[StrPath] = None):
        # validate using cwl.utils
        if not isinstance(clt_path, (Path, str)):
            raise TypeError("cwl_path must be a Path or str")
        clt_path_ = Path(clt_path) if isinstance(clt_path, str) else clt_path
        stepid = StepId(clt_path_.stem, 'global')

        if clt_path_.exists():
            try:
                clt = load_document_by_uri(clt_path_)
            except Exception as exc:
                raise InvalidCLTError(f"invalid cwl file: {clt_path_}") from exc
            with clt_path_.open("r", encoding="utf-8") as file:
                yaml_file = yaml.safe_load(file)

            # Add the Tool to the global config dictionary. See explanation below.
            global_config[stepid] = Tool(clt_path_.stem, yaml_file)
        elif stepid in global_config:
            # Use the path fallback mechanism.
            # In other words, if the hardcoded, nonportable, system-dependent paths
            # are not found above (i.e. because someone else is trying to run
            # the workflow on another machine), then try to load the CLT from
            # a global dictionary, which can be pre-configured to have paths
            # that exist on the target machine.
            # Again, global_config is just a dictionary; it couldn't be more pythonic.
            # You can initialize it however you want (or not at all!).
            tool = global_config[stepid]

            logger.info(f'{clt_path_} does not exist, but {clt_path_.stem} was found in the global config.')
            logger.info(f'Using file contents from {tool.run_path}')

            yaml_file = tool.cwl
            clt = load_document_by_yaml(yaml_file, tool.run_path)
        else:
            logger.warning(f'Warning! {clt_path_} does not exist, and')
            logger.warning(f'{clt_path_.stem} was not found in the global config.')
            raise InvalidCLTError(f"invalid cwl file: {clt_path_}")

        if config_path:
            cfg_path_ = Path(config_path) if isinstance(config_path, str) else config_path
            with cfg_path_.open("r", encoding="utf-8") as file:
                cfg_yaml = yaml.safe_load(file)
        else:
            cfg_yaml = _default_dict()  # redundant, to avoid it being unbound
        process_name = clt_path_.stem
        data = {
            "clt": clt,
            "clt_path": clt_path_,
            "cwl_version": clt.cwlVersion,
            "process_name": process_name,
            "inputs": clt.inputs,
            "outputs": clt.outputs,
            "yaml": yaml_file,
            "cfg_yaml": cfg_yaml,
        }
        super().__init__(**data)
        for inp in self.inputs:
            inp.parent_obj = self  # Create a reference to the parent Step object
        self._input_names = [inp.id.split("#")[-1] for inp in clt.inputs]
        for out in self.outputs:
            out.parent_obj = self  # Create a reference to the parent Step object
        self._output_names = [out.id.split("#")[-1] for out in clt.outputs]
        if config_path:
            self._set_from_io_cfg()

    @field_validator("inputs", mode="before")
    @classmethod
    def cast_to_process_input_model(
        cls, cwl_inps: list[CWLInputParameter]
    ) -> list[ProcessInput]:
        """Populate inputs from cwl.inputs."""
        # NOTE: .type in version 0.31 only!
        # NOTE: Cannot initialize .parent_obj = self here due to @classmethod
        return [ProcessInput(str(x.id), x.type_) for x in cwl_inps]

    @field_validator("outputs", mode="before")
    @classmethod
    def cast_to_process_output_model(
        cls, cwl_outs: list[CWLOutputParameter]
    ) -> list[ProcessOutput]:
        """Populate outputs from cwl.outputs."""
        # NOTE: .type in version 0.31 only!
        # NOTE: Cannot initialize .parent_obj = self here due to @classmethod
        return [ProcessOutput(str(x.id), x.type_) for x in cwl_outs]

    def __repr__(self) -> str:
        repr_ = f"Step(clt_path={self.clt_path.__repr__()})"
        return repr_

    def __setattr__(self, __name: str, __value: Any) -> Any:
        if __name in ["inputs", "outputs", "yaml", "cfg_yaml", "process_name", "_input_names", "_output_names",
                      "__private_attributes__", "__pydantic_private__"]:
            return super().__setattr__(__name, __value)
        if __name == "scatterMethod":
            if hasattr(ScatterMethod, __value):
                return super().__setattr__(__name, __value)
            else:
                raise ValueError(
                    f"Invalid value for scatterMethod the valid values are : \n {ScatterMethod.dotproduct.value} "
                    f"{ScatterMethod.flat_crossproduct.value} {ScatterMethod.nested_crossproduct.value}\n")
        if __name == "scatter":
            if not all([isinstance(x, ProcessInput) for x in __value]):
                raise TypeError("all scatter inputs must be ProcessInput type")
            return super().__setattr__(__name, __value)
        if __name == "when":
            if (isinstance(__value, str)) and __value.startswith('$(') and __value.endswith(')'):
                return super().__setattr__(__name, __value)
            else:
                raise ValueError("Invalid input to when.The js string must start with '$(' and end with ')'")

        if hasattr(self, "_input_names") and __name in self._input_names:
            set_input_Step_Workflow(self, __name, __value)
        else:
            return super().__setattr__(__name, __value)

    def __getattr__(self, __name: str) -> Any:
        if __name in ["__pydantic_private__", "__class__", "__private_attributes__"]:
            return super().__getattribute__(__name)
        if __name != "_output_names" and __name in self._output_names:  # and hasattr(self, "_output_names") ?
            return self.outputs[self._output_names.index(__name)]
        # https://github.com/pydantic/pydantic/blob/812516d71a8696d5e29c5bdab40336d82ccde412/pydantic/main.py#L743-744
        # "We put `__getattr__` in a non-TYPE_CHECKING block because otherwise, mypy allows arbitrary attribute access
        # The same goes for __setattr__ and __delattr__, see: https://github.com/pydantic/pydantic/issues/8643"
        return super().__getattr__(__name)  # type: ignore

    def _set_from_io_cfg(self) -> None:
        for name, value in self.cfg_yaml.items():
            value_ = _get_value_from_cfg(value)
            setattr(self, name, value_)

    def _validate(self) -> None:
        for inp in self.inputs:
            if inp.required and inp.value is None:
                raise MissingRequiredValueError(f"{inp.name} is required")

    @property
    def _yml(self) -> dict:
        in_dict: dict[str, Any] = {}  # NOTE: input values can be arbitrary JSON; not just strings!
        for inp in self.inputs:
            if inp.value is not None:
                if isinstance(inp.value, Path):
                    # Special case for Path since it does not inherit from YAMLObject
                    in_dict[inp.name] = str(inp.value)
                elif isinstance(inp.value, dict) and isinstance(inp.value.get('wic_alias', {}), Path):
                    # Special case for Path since it does not inherit from YAMLObject
                    in_dict[inp.name] = {'wic_alias': str(inp.value['wic_alias'])}
                elif isinstance(inp.value, dict) and isinstance(inp.value.get('wic_inline_input', {}), Path):
                    # Special case for Path since it does not inherit from YAMLObject
                    in_dict[inp.name] = {'wic_inline_input': str(inp.value['wic_inline_input'])}
                elif isinstance(inp.value, dict) and isinstance(inp.value.get('wic_inline_input', {}), str):
                    # Special case for inline str since it does not inherit from YAMLObject
                    in_dict[inp.name] = {'wic_inline_input': inp.value.get('wic_inline_input')}
                elif isinstance(inp.value, str):
                    in_dict[inp.name] = inp.value  # Obviously strings are serializable
                elif isinstance(inp.value, yaml.YAMLObject):
                    # Serialization and deserialization logic should always be
                    # encapsulated within each object. For the pyyaml library,
                    # each object should inherit from pyyaml.YAMLObject.
                    # See https://pyyaml.org/wiki/PyYAMLDocumentation
                    # Section "Constructors, representers, resolvers"
                    # class Monster(yaml.YAMLObject): ...
                    in_dict[inp.name] = inp.value
                else:
                    logger.warning(f'Warning! input name {inp.name} input value {inp.value}')
                    logger.warning('is not an instance of YAMLObject. The default str() serialization')
                    logger.warning('logic often gives bad results. Please explicitly inherit from YAMLObject.')
                    in_dict[inp.name] = inp.value

        out_list: list = []  # The out: tag is a list, not a dict
        out_list = [{out.name: out.value} for out in self.outputs if out.value]
        # list of inputs to be scattered on
        scatter_list: list = []
        scatter_list = [sc_inp.name for sc_inp in self.scatter]

        d = {
            "id": self.process_name,
            "in": in_dict,
            "out": out_list,
        }
        # scatter operates on input sink
        if self.scatter:
            d["scatter"] = scatter_list
            if '' == self.scatterMethod:
                self.scatterMethod = ScatterMethod.dotproduct.value
            d["scatterMethod"] = self.scatterMethod
        # when operates on step
        if self.when != '':
            d["when"] = self.when
        return d

    def _save_cwl(self, path: Path) -> None:
        cwl_adapters = path.joinpath("cwl_adapters")
        cwl_adapters.mkdir(exist_ok=True, parents=True)
        with open(
            cwl_adapters.joinpath(f"{self.process_name}.cwl"),
            "w",
            encoding="utf-8",
        ) as file:
            file.write(yaml.dump(self.yaml))


def extract_tools_paths_NONPORTABLE(steps: list[Step]) -> Tools:
    """extract a Tools global configuration object from the NONPORTABLE paths hardcoded in the Steps.

    Args:
        steps (list[Step]): A list of Steps.

    Returns:
        Tools: A Tools object, which is just a dictionary mapping a globally unique
        (i.e. portable) identifier to a system-dependendent Path.
    """
    return {StepId(step.process_name, 'global'): Tool(str(step.clt_path), step.yaml) for step in steps}


# Analogous to cu_parser.Process, we want to create our own Union. However,
# Process = Union[Step, Workflow]  # Cannot define Process before Workflow


class Workflow(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(arbitrary_types_allowed=True)

    steps: list  # list[Process]  # and cannot use Process defined after Workflow within a Workflow
    process_name: str
    user_args: list[str]
    inputs: list[ProcessInput] = []
    outputs: list[ProcessOutput] = []
    _input_names: list[str] = PrivateAttr(default_factory=list)
    _output_names: list[str] = PrivateAttr(default_factory=list)
    yml_path: Optional[Path] = Field(default=None)
    # Cannot use field() from dataclasses. Otherwise:
    # from dataclasses import field
    # field(default=None, init=False, repr=False)
    # TypeError: 'ModelPrivateAttr' object is not iterable

    def __init__(self, steps: list, workflow_name: str, user_args: list[str] = []):
        data = {
            "process_name": workflow_name,
            "steps": steps,
            "user_args": user_args
        }
        super().__init__(**data)

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        # Only the root workflow should have all required inputs and can be validated.
        # Cannot validate subworkflows because by definition,
        # subworkflows will NOT have all required inputs.
        for s in self.steps:
            try:
                if isinstance(s, Step):
                    s._validate()  # pylint: disable=W0212
                if isinstance(s, Workflow):
                    # recursively validate subworkflows ?
                    s._validate()  # pylint: disable=W0212
            except BaseException as exc:
                raise InvalidStepError(
                    f"{s.process_name} is missing required inputs"
                ) from exc

    def append(self, step_: Step) -> None:
        """Append step to Workflow."""
        if not isinstance(step_, (Step, Workflow)):
            raise TypeError("step must be either a Step or a Workflow")
        self.steps.append(step_)

        # for name in step_._input_names:
        #     self._input_names.append(name)
        # for inp in step_.inputs:
        #     self.inputs.append(inp)

        # for name in step_._output_names:
        #     self._output_names.append(name)
        # for out in step_.outputs:
        #     self.outputs.append(out)

    @property
    def yaml(self) -> dict[str, Any]:
        """Converts a Workflow to the equivalent underlying WIC YML representation, in-memory."""
        # NOTE: Use the CWL v1.2 `Any` type for lack of actual type information.
        # TODO: try inp.inp_type (if initialized?)
        inputs: dict[str, Any] = {inp.name: {'type': 'Any'} for inp in self.inputs}
        # TODO: outputs?
        steps = []
        for s in self.steps:
            if isinstance(s, Step):
                steps.append(s._yml)
            elif isinstance(s, Workflow):
                ins = {
                    inp.name: inp.value
                    for inp in s.inputs
                    if inp.value is not None  # Subworkflow args are not required
                }
                parentargs: dict[str, Any] = {"in": ins} if ins else {}
                # See the second to last line of ast.read_ast_from_disk()
                d = {'id': self.process_name + '.wic',
                     'subtree': s.yaml,  # recursively call .yaml (i.e. on s, not self)
                     'parentargs': parentargs}
                steps.append(d)
            #  else: ...
        yaml_contents = {"inputs": inputs, "steps": steps} if inputs else {"steps": steps}
        return yaml_contents

    def write_ast_to_disk(self, directory: Path) -> None:
        """Converts a Workflow to the equivalent underlying WIC YML representation,
        and writes each subworkflow to disk as a separate yml file.

        Args:
            directory (Path): The directory to write the yml file(s).
        """
        # NOTE: Use the CWL v1.2 `Any` type for lack of actual type information.
        # TODO: try inp.inp_type (if initialized?)
        inputs: dict[str, Any] = {inp.name: {'type': 'Any'} for inp in self.inputs}
        # TODO: outputs?
        steps = []
        for s in self.steps:
            if isinstance(s, Step):
                steps.append(s._yml)
            elif isinstance(s, Workflow):
                ins = {
                    inp.name: inp.value
                    for inp in s.inputs
                    if inp.value is not None  # Subworkflow args are not required
                }
                parentargs: dict[str, Any] = {"in": ins} if ins else {}
                s.write_ast_to_disk(directory)  # recursively call
                steps.append({'id': s.process_name + '.wic', **parentargs})
            #  else: ...
        yaml_contents = {"inputs": inputs, "steps": steps} if inputs else {"steps": steps}
        # NOTE: For various reasons, process_name should be globally unique.
        # In this case, it is to avoid overwriting files.
        directory.mkdir(exist_ok=True, parents=True)
        with open(str(directory / self.process_name) + '.wic', mode='w', encoding='utf-8') as f:
            f.write(yaml.dump(yaml_contents, sort_keys=False, line_break='\n', indent=2))

    def add_input(self, __name: str) -> Any:
        # Assume the user wants to auto-generate an input on the fly
        # Ideally, we need to figure out how to distinguish whether an attribute
        # workflow = Workflow(...)
        # workflow.foo
        # workflow.bar
        # is an explicit input and/or
        # workflow.workflowname__step__1__stepname
        # an auto-generated internal name
        # (See https://workflow-inference-compiler.readthedocs.io/en/latest/dev/algorithms.html#namespacing)
        # or if it is just a regular python field/method, etc.
        # workflow.__repr__
        # (and thus has nothing to do with CWL and should NOT be added as an input)
        # For now, let's completely ignore that distinction, and hopefully
        # the user never wants to get or set built-in python attributes!
        logger.warning(f'Adding a new input {__name} to workflow {self.process_name}')
        self._input_names.append(__name)
        inp = ProcessInput(__name, 'Any')  # Use Any type
        inp.parent_obj = self  # Create a reference to the parent Workflow object
        self.inputs.append(inp)
        return inp

    def add_output(self, __name: str) -> Any:
        logger.warning(f'Adding a new output {__name} to workflow {self.process_name}')
        self._output_names.append(__name)
        out = ProcessOutput(__name, 'Any')  # Use Any type
        out.parent_obj = self  # Create a reference to the parent Workflow object
        self.outputs.append(out)
        return out

    # def __repr__(self) -> str:
    #     repr_ = ...
    #     return repr_

    def __setattr__(self, __name: str, __value: Any) -> Any:
        if __name in ["inputs", "outputs", "yaml", "cfg_yaml", "process_name", "_input_names", "_output_names",
                      "__private_attributes__", "__pydantic_private__"]:
            return super().__setattr__(__name, __value)
        # NOTE: Unlike the syntax `... = workflow.attribute`
        # where attribute is interpreted to be a formal parameter / an input variable for the workflow,
        # the syntax `workflow.attribute = ...` is ambiguous.
        # attribute could refer to the actual parameter / input value, or
        # attribute could refer to a formal output variable for the workflow.
        # By default, we have arbitrarily chosen the former.
        if hasattr(self, "_input_names"):
            if __name not in self._input_names:
                self.add_input(__name)
            set_input_Step_Workflow(self, __name, __value)
        else:
            return super().__setattr__(__name, __value)

    def __getattr__(self, __name: str) -> Any:
        if __name in ["__pydantic_private__", "__class__", "__private_attributes__"]:
            return super().__getattribute__(__name)
        # TODO: double check the following logic.
        if __name != "_input_names" and __name != "_output_names":  # and hasattr(self, "_output_names") ?
            if __name in self._output_names:
                return self.outputs[self._output_names.index(__name)]
            else:
                return self.add_output(__name)
        # https://github.com/pydantic/pydantic/blob/812516d71a8696d5e29c5bdab40336d82ccde412/pydantic/main.py#L743-744
        # "We put `__getattr__` in a non-TYPE_CHECKING block because otherwise, mypy allows arbitrary attribute access
        # The same goes for __setattr__ and __delattr__, see: https://github.com/pydantic/pydantic/issues/8643"
        return super().__getattr__(__name)  # type: ignore

    def _save_yaml(self) -> None:
        _WIC_PATH.mkdir(parents=True, exist_ok=True)
        self.yml_path = _WIC_PATH.joinpath(f"{self.process_name}.wic")
        with open(f"{self.process_name}.wic", "w", encoding="utf-8") as file:
            file.write(yaml.dump(self.yaml))

    def _save_all_cwl(self) -> None:
        """Save CWL files to cwl_adapters.

        This is necessary for WIC to compile the workflow.
        """
        _WIC_PATH.mkdir(parents=True, exist_ok=True)
        for s in self.steps:
            try:
                if isinstance(s, Step):
                    s._save_cwl(_WIC_PATH)  # pylint: disable=W0212
            except BaseException as exc:
                raise exc

    def flatten_steps(self) -> list[Step]:
        """Flattens all Steps into a linear list. This is similar, but different, from inlineing.

        Returns:
            list[Step]: a linear list of Steps
        """
        steps = []
        for step in self.steps:
            if isinstance(step, Step):
                steps.append(step)
            if isinstance(step, Workflow):
                steps += step.flatten_steps()
        return steps

    # NOTE: Cannot return list[Workflow] because Workflow is not yet defined.
    def flatten_subworkflows(self) -> list:
        """Flattens all sub-Workflows into a linear list. The root workflow will be at index 0,
        i.e. to get a list of all proper subworkflows, just use [1:]

        Returns:
            list: a linear list of sub-Workflows.
        """
        subworkflows = [self]
        for step in self.steps:
            if isinstance(step, Workflow):
                subworkflows += step.flatten_subworkflows()
        return subworkflows

    def compile(self, write_to_disk: bool = False) -> CompilerInfo:
        """Compile Workflow using WIC.

        Args:
            write_to_disk (bool, optional): Save the compiled CWL Workflow to disk. Defaults to False.

        Returns:
            CompilerInfo: Contains the data associated with compiled subworkflows\n
            (in the Rose Tree) together with mutable cumulative environment\n
            information which needs to be passed through the recursion.
        """
        global global_config
        self._validate()
        args = get_args(self.process_name, self.user_args)  # Use mock CLI args

        graph = get_graph_reps(self.process_name)
        yaml_tree = YamlTree(StepId(self.process_name, 'global'), self.yaml)

        # NOTE: This is critical for control flow in the compiler. See utils.get_subkeys()
        steps_config = extract_tools_paths_NONPORTABLE(self.flatten_steps())
        global_config = merge(steps_config, global_config, strategy=Strategy.TYPESAFE_REPLACE)

        # The compile_workflow function is 100% in-memory
        compiler_info = compiler.compile_workflow(yaml_tree, args, [], [graph], {}, {}, {}, {},
                                                  global_config, True, relative_run_path=True, testing=False)

        if write_to_disk:
            # Now we can choose whether to write_to_disk or not
            rose_tree: RoseTree = compiler_info.rose
            input_output.write_to_disk(rose_tree, Path('autogenerated/'), True, args.inputs_file)

        return compiler_info

    def get_cwl_workflow(self) -> Json:
        """Return the compiled Cwl and its inputs in one payload Json
        Returns:
            Json: Contains the compiled CWL and yaml inputs to the workflow.
        """
        compiler_info = self.compile(write_to_disk=False)
        args = get_args(self.process_name, self.user_args)
        rose_tree = compiler_info.rose

        # this is unfortunately necessary for now
        # TODO: uncouple dumping to file and getting cwl steps
        # then remove this write_to_disk call here
        input_output.write_to_disk(rose_tree, Path('autogenerated/'), True, args.inputs_file)

        rose_tree = post_compile.cwl_inline_runtag(rose_tree)
        sub_node_data = rose_tree.data
        cwl_ast = sub_node_data.compiled_cwl

        # Copy samee's workaround for duplicate outs
        for step in cwl_ast['steps']:
            out_vars = step['out']
            out_vars_unique = list(set(out_vars))
            out_vars_unique.sort()
            step['out'] = out_vars_unique

        yaml_inputs = sub_node_data.workflow_inputs_file
        workflow_json: Json = {}
        workflow_json = {
            "name": self.process_name,
            "yaml_inputs": yaml_inputs,
            **cwl_ast
        }
        return workflow_json

    def run(self) -> None:
        """Run compiled workflow."""
        logger.info(f"Running {self.process_name}")
        plugins.logging_filters()
        compiler_info = self.compile(write_to_disk=True)
        args = get_args(self.process_name, self.user_args)  # Use mock CLI args
        rose_tree: RoseTree = compiler_info.rose

        post_compile.cwl_docker_extract(args.container_engine, args.pull_dir, self.process_name)
        if args.docker_remove_entrypoints:
            rose_tree = post_compile.remove_entrypoints(args.container_engine, rose_tree)
        post_compile.find_and_create_output_dirs(rose_tree)
        # Do NOT capture stdout and/or stderr and pipe warnings and errors into a black hole.
        retval = run_local_module.run_local(args, rose_tree, args.cachedir, args.cwl_runner, True)

        # Finally, since there is an output file copying bug in cwltool,
        # we need to copy the output files manually. See comment above.
        if args.cwl_runner == 'cwltool' and args.copy_output_files:
            run_local_module.copy_output_files(self.process_name)

# Process = Union[Step, Workflow]
