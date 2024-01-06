"""Query Pipeline."""

import uuid
from functools import cmp_to_key
from typing import Any, Dict, List, Optional, Sequence, Set

from llama_index.bridge.pydantic import BaseModel, Field
from llama_index.callbacks import CallbackManager
from llama_index.core.query_pipeline.query_component import (
    InputKeys,
    OutputKeys,
    QueryComponent,
)


class InputTup(BaseModel):
    """Input Tuple."""

    class Config:
        arbitrary_types_allowed = True

    module_key: str
    module: QueryComponent
    input: Dict[str, Any]


class Link(BaseModel):
    """Link between two modules."""

    class Config:
        arbitrary_types_allowed = True

    src: str
    dest: str
    src_key: Optional[str] = None
    dest_key: Optional[str] = None


# class ModuleDepsInputs(BaseModel):
#     """Module Inputs from Upstream Dependencies."""
#     module_key: str
#     input_dict: Dict[str, Any]


def is_ancestor(
    module_key: str, child_module_key: str, edge_dict: Dict[str, List[Link]]
) -> bool:
    """Check if module is ancestor of another module."""
    if module_key == child_module_key:
        raise ValueError("Module cannot be ancestor of itself.")
    if module_key not in edge_dict:
        return False
    for link in edge_dict[module_key]:
        if is_ancestor(link.dest, child_module_key, edge_dict):
            return True
    return False


def add_output_to_module_inputs(
    link: Link,
    output_dict: Dict[str, Any],
    module_input_keys: InputKeys,
    module_inputs: Dict[str, Any],
) -> None:
    """Add input to module deps inputs."""
    # get relevant output from link
    if link.src_key is None:
        # ensure that output_dict only has one key
        if len(output_dict) != 1:
            raise ValueError("Output dict must have exactly one key.")
        output = list(output_dict.values())[0]
    else:
        output = output_dict[link.src_key]

    # now attach output to relevant input key for module
    if link.dest_key is None:
        # ensure that module_input_keys only has one key
        if len(module_input_keys) != 1:
            raise ValueError("Module input keys must have exactly one key.")
        module_inputs[list(module_input_keys.all())[0]] = output
    else:
        module_inputs[link.dest_key] = output


class QueryPipeline(QueryComponent):
    """A query pipeline that can allow arbitrary chaining of different modules.

    A pipeline itself is a query component, and can be used as a module in another pipeline.

    """

    callback_manager: CallbackManager = Field(
        default_factory=lambda: CallbackManager([]), exclude=True
    )

    module_dict: Dict[str, QueryComponent] = Field(
        default_factory=dict, description="The modules in the pipeline."
    )
    edge_dict: Dict[str, List[Link]] = Field(
        default_factory=dict, description="The edges in the pipeline."
    )

    # root_keys: List[str] = Field(
    #     default_factory=list, description="The keys of the root modules."
    # )

    class Config:
        arbitrary_types_allowed = True

    def __init__(
        self,
        callback_manager: Optional[CallbackManager] = None,
        chain: Optional[Sequence[QueryComponent]] = None,
        **kwargs: Any,
    ):
        # self.callback_manager = callback_manager or CallbackManager([])
        # self.module_dict: Dict[str, QueryComponent] = {}
        # self.edge_dict: Dict[str, List[Link]] = {}
        # self.root_keys: List[str] = []
        super().__init__(
            callback_manager=callback_manager,
            **kwargs,
        )

        if chain is not None:
            # generate implicit link between each item, add
            self.add_chain(chain)

    def add_chain(self, chain: Sequence[QueryComponent]) -> None:
        """Add a chain of modules to the pipeline.

        This is a special form of pipeline that is purely sequential/linear.
        This allows a more concise way of specifying a pipeline.

        """
        # first add all modules
        module_keys = []
        for module in chain:
            module_key = str(uuid.uuid4())
            self.add(module_key, module)
            module_keys.append(module_key)

        # then add all links
        for i in range(len(chain) - 1):
            self.add_link(src=module_keys[i], dest=module_keys[i + 1])

    def add_modules(self, module_dict: Dict[str, QueryComponent]) -> None:
        """Add modules to the pipeline."""
        for module_key, module in module_dict.items():
            self.add(module_key, module)

    def add(self, module_key: str, module: QueryComponent) -> None:
        """Add a module to the pipeline."""
        # if already exists, raise error
        if module_key in self.module_dict:
            raise ValueError(f"Module {module_key} already exists in pipeline.")
        self.module_dict[module_key] = module

    def add_link(
        self,
        src: str,
        dest: str,
        src_key: Optional[str] = None,
        dest_key: Optional[str] = None,
    ) -> None:
        """Add a link between two modules."""
        if src not in self.module_dict:
            raise ValueError(f"Module {src} does not exist in pipeline.")
        if src not in self.edge_dict:
            self.edge_dict[src] = []
        self.edge_dict[src].append(
            Link(src=src, dest=dest, src_key=src_key, dest_key=dest_key)
        )

    def _get_root_keys(self) -> List[str]:
        """Get root keys."""
        # first add all to root keys, then remove
        root_keys: Set[str] = set(self.module_dict.keys())
        # get all modules without upstream dependencies
        for module_key in self.edge_dict.keys():
            for link in self.edge_dict[module_key]:
                if link.dest in root_keys:
                    root_keys.remove(link.dest)
        return list(root_keys)

    def _get_leaf_keys(self) -> List[str]:
        """Get leaf keys."""
        # get all modules without downstream dependencies
        leaf_keys = []
        for module_key in self.module_dict.keys():
            if module_key not in self.edge_dict or len(self.edge_dict[module_key]) == 0:
                leaf_keys.append(module_key)
        return list(leaf_keys)

    def _ancestral_sort(self, queue: List[InputTup]) -> List[InputTup]:
        """Sort queue by ancestral order of the modules.

        Why do we want to do this? Especially if modules are only inserted to the queue
        if they have all their required dependencies satisfied?

        Because each module has optional dependencies as well, and it's not really clear whether
        or not that optional dependency will be satisfied (or left blank).

        The way to get around this is to sort the queue by ancestral order, so that the module
        furthest upstream is run first, and then the modules further downstream are run later.
        That way you can be sure that any optional dependencies that should be satisfied
        will be satisifed by the time that module is run.

        """

        # define comparator function
        def _cmp(input_tup1: InputTup, input_tup2: InputTup) -> int:
            """Compare based on whether is_ancestor is true."""
            if is_ancestor(
                input_tup1.module_key, input_tup2.module_key, self.edge_dict
            ):
                return -1
            elif is_ancestor(
                input_tup2.module_key, input_tup1.module_key, self.edge_dict
            ):
                return 1
            else:
                # they're not ancestors of each other (e.g. on parallel branches), so return 0
                return 0

        return sorted(queue, key=cmp_to_key(_cmp))

    def run(self, *args: Any, **kwargs: Any) -> Any:
        """Run the pipeline.

        Assume that there is a single root module and a single output module.

        For multi-input and multi-outputs, please see `run_multi`.

        """
        # currently assume kwargs, add handling for args later
        ## run pipeline
        ## assume there is only one root - for multiple roots, need to specify `run_multi`
        root_keys = self._get_root_keys()
        if len(root_keys) != 1:
            raise ValueError("Only one root is supported.")
        root_key = root_keys[0]
        root_module = self.module_dict[root_key]

        # for each module in queue, run it, and then check if its edges have all their dependencies satisfied
        # if so, add to queue
        queue: List[InputTup] = [
            InputTup(module_key=root_key, module=root_module, input=kwargs)
        ]

        # module_deps_inputs is a dict to collect inputs for a module
        # mapping of module_key -> dict of input_key -> input
        # initialize with blank dict for every module key
        # the input dict of each module key will be populated as the upstream modules are run
        all_module_inputs: Dict[str, Dict[str, Any]] = {
            module_key: {} for module_key in self.module_dict.keys()
        }
        result_outputs: List[Any] = []
        while len(queue) > 0:
            input_tup = queue.pop(0)
            module_key, module, input = (
                input_tup.module_key,
                input_tup.module,
                input_tup.input,
            )

            print(f"Running module {module_key} with input {input}")
            output_dict = module.run_component(**input)

            # if there's no more edges, add result to output
            if module_key not in self.edge_dict:
                result_outputs.append(output_dict)
            else:
                for link in self.edge_dict[module_key]:
                    edge_module = self.module_dict[link.dest]

                    # add input to module_deps_inputs
                    add_output_to_module_inputs(
                        link,
                        output_dict,
                        edge_module.input_keys,
                        all_module_inputs[link.dest],
                    )
                    if len(all_module_inputs[link.dest]) == len(edge_module.input_keys):
                        queue.append(
                            InputTup(
                                module_key=link.dest,
                                module=edge_module,
                                input=all_module_inputs[link.dest],
                            )
                        )

            # sort queue by ancestral order of the modules
            # see docstring as for why
            queue = self._ancestral_sort(queue)

        if len(result_outputs) != 1:
            raise ValueError("Only one output is supported.")

        result_output = result_outputs[0]
        # if it's a dict with one key, return the value
        if isinstance(result_output, dict) and len(result_output) == 1:
            return list(result_output.values())[0]
        else:
            return result_output

    def run_multi(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Run the pipeline for multiple roots."""
        raise NotImplementedError("Not implemented yet.")

    def _validate_component_inputs(self, input: Dict[str, Any]) -> Dict[str, Any]:
        """Validate component inputs during run_component."""
        return input

    def _run_component(self, **kwargs: Any) -> Dict[str, Any]:
        """Run component."""
        return self.run(**kwargs)

    def input_keys(self) -> InputKeys:
        """Input keys."""
        # get input key of first module
        root_keys = self._get_root_keys()
        if len(root_keys) != 1:
            raise ValueError("Only one root is supported.")
        root_module = self.module_dict[root_keys[0]]
        return root_module.input_keys

    def output_keys(self) -> OutputKeys:
        """Output keys."""
        # get output key of last module
        leaf_keys = self._get_leaf_keys()
        if len(leaf_keys) != 1:
            raise ValueError("Only one leaf is supported.")
        leaf_module = self.module_dict[leaf_keys[0]]
        return leaf_module.output_keys
