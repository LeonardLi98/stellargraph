# -*- coding: utf-8 -*-
#
# Copyright 2017-2020 Data61, CSIRO
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import itertools
from collections import defaultdict

import numpy as np
import pandas as pd
import scipy.sparse as sps

from ..globalvar import SOURCE, TARGET, WEIGHT
from .validation import require_dataframe_has_columns, comma_sep


class ExternalIdIndex:
    """
    An ExternalIdIndex maps between "external IDs" and "integer locations" or "internal locations"
    (ilocs).

    It is designed to allow handling only efficient integers internally, but easily convert between
    them and the user-facing IDs.
    """

    def __init__(self, ids):
        self._index = pd.Index(ids)
        # reserve 2 ^ (n-bits) - 1 for sentinel
        self.dtype = np.min_scalar_type(len(self._index))

        if not self._index.is_unique:
            # had some duplicated IDs, which is an error
            duplicated = self._index[self._index.duplicated()].unique()
            raise ValueError(
                f"expected IDs to appear once, found some that appeared more: {comma_sep(duplicated)}"
            )

    @property
    def pandas_index(self) -> pd.Index:
        """
        Return a collection of all the elements contained in this index.
        """
        return self._index

    def __len__(self):
        return len(self._index)

    def contains_external(self, id):
        """
        Whether the external ID is indexed by this ``ExternalIdIndex``.
        """
        return id in self._index

    def is_valid(self, ilocs: np.ndarray) -> np.ndarray:
        """
        Flags the locations of all the ilocs that are valid (that is, where to_iloc didn't fail).
        """
        return (0 <= ilocs) & (ilocs < len(self))

    def require_valid(self, query_ids, ilocs: np.ndarray) -> np.ndarray:
        valid = self.is_valid(ilocs)

        if not valid.all():
            missing_values = np.asarray(query_ids)[~valid]

            if len(missing_values) == 1:
                raise KeyError(missing_values[0])

            raise KeyError(missing_values)

    def to_iloc(self, ids, smaller_type=True, strict=False) -> np.ndarray:
        """
        Convert external IDs ``ids`` to integer locations.

        Args:
            ids: a collection of external IDs
            smaller_type: if True, convert the ilocs to the smallest type that can hold them, to reduce storage
            strict: if True, check that all IDs are known and throw a KeyError if not

        Returns:
            A numpy array of the integer locations for each id that exists, with missing IDs
            represented by either the largest value of the dtype (if smaller_type is True) or -1 (if
            smaller_type is False)
        """
        internal_ids = self._index.get_indexer(ids)
        if strict:
            self.require_valid(ids, internal_ids)

        # reduce the storage required (especially useful if this is going to be stored rather than
        # just transient)
        if smaller_type:
            return internal_ids.astype(self.dtype)
        return internal_ids

    def from_iloc(self, internal_ids) -> np.ndarray:
        """
        Convert integer locations to their corresponding external ID.
        """
        return self._index.to_numpy()[internal_ids]


class ElementData:
    """
    An ``ElementData`` stores "shared" information about a set of a graph elements (nodes or
    edges). Elements of every type must have this information, such as the type itself or the
    source, target and weight for edges.

    It indexes these in terms of ilocs (see :class:`ExternalIdIndex`). The data is stored as columns
    of raw numpy arrays, because indexing such arrays is significantly (orders of magnitude) faster
    than indexing pandas dataframes, series or indices.

    Args:
        ids (sequence): the IDs of each element
        type_starts (list of tuple of type name, int): the starting iloc of the elements of each type within ``shared``
    """

    def __init__(self, ids, type_starts):
        if not isinstance(type_starts, list):
            raise TypeError(
                f"type_starts: expected list, found {type(type_starts).__name__}"
            )

        type_ranges = {}
        type_stops = type_starts[1:] + [(None, len(ids))]
        consecutive_types = zip(type_starts, type_stops)
        for idx, ((type_name, start), (_, stop)) in enumerate(consecutive_types):
            if idx == 0 and start != 0:
                raise ValueError(
                    f"type_starts: expected first type ({type_name!r}) to start at index 0, found start {start}"
                )
            if start > stop:
                raise TypeError(
                    f"type_starts (for {type_name!r}): expected valid type range, found start ({start}) after stop ({stop})"
                )
            type_ranges[type_name] = range(start, stop)

        self._id_index = ExternalIdIndex(ids)

        # there's typically a small number of types, so we can map them down to a small integer type
        # (usually uint8) for minimum storage requirements
        all_types = [type_name for type_name, _ in type_starts]
        type_sizes = [len(type_ranges[type_name]) for type_name in all_types]

        self._type_index = ExternalIdIndex(all_types)
        self._type_column = self._type_index.to_iloc(all_types).repeat(type_sizes)
        self._type_element_ilocs = type_ranges

    def __len__(self) -> int:
        return len(self._id_index)

    def __contains__(self, item) -> bool:
        return self._id_index.contains_external(item)

    @property
    def ids(self) -> ExternalIdIndex:
        """
        Returns:
             All of the IDs of these elements.
        """
        return self._id_index

    @property
    def types(self) -> ExternalIdIndex:
        """
        Returns:
            All the type names of these elements.
        """
        return self._type_index

    def type_range(self, type_name):
        """
        Returns:
            A range over the ilocs of the given type name
        """
        return self._type_element_ilocs[type_name]

    @property
    def type_ilocs(self) -> np.ndarray:
        """
        Returns:
            A numpy array with the type of each element, stores as the raw iloc of that type.
        """
        return self._type_column

    def type_of_iloc(self, id_ilocs) -> np.ndarray:
        """
        Return the types of the ID(s).

        Args:
            id_ilocs: a "selector" based on the element ID integer locations

        Returns:
             A sequence of types, corresponding to each of the ID(s) integer locations
        """
        type_codes = self._type_column[id_ilocs]
        return self._type_index.from_iloc(type_codes)


class NodeData(ElementData):
    """
    Args:
        ids (sequence): the IDs of each element
        type_starts (list of tuple of type name, int): the starting iloc of the nodes of each type within ``shared``
        features (dict of type name to numpy array): a 2D numpy or scipy array of feature vectors for the nodes of each type
    """

    def __init__(self, ids, type_starts, features):
        super().__init__(ids, type_starts)
        if not isinstance(features, dict):
            raise TypeError(f"features: expected dict, found {type(features).__name__}")

        for key, data in features.items():
            if not isinstance(data, (np.ndarray, sps.spmatrix)):
                raise TypeError(
                    f"features[{key!r}]: expected numpy or scipy array, found {type(data).__name__}"
                )

            if len(data.shape) != 2:
                raise ValueError(
                    f"features[{key!r}]: expected 2 dimensions, found {len(data.shape)}"
                )

            rows, _columns = data.shape
            expected = len(self._type_element_ilocs[key])
            if rows != expected:
                raise ValueError(
                    f"features[{key!r}]: expected one feature per ID, found {expected} IDs and {rows} feature rows"
                )

        self._features = features

    def features_of_type(self, type_name) -> np.ndarray:
        """
        Returns all features for a given type.

        Args:
            type_name (hashable): the name of the type
        """
        return self._features[type_name]

    def features(self, type_name, id_ilocs) -> np.ndarray:
        """
        Return features for a set of IDs within a given type.

        Args:
            type_name (hashable): the name of the type for all of the IDs
            ids (iterable of IDs): a sequence of IDs of elements of type type_name

        Returns:
            A 2D numpy array, where the rows correspond to the ids
        """
        start = self._type_element_ilocs[type_name].start
        feature_ilocs = id_ilocs - start

        # FIXME: better error messages
        if (feature_ilocs < 0).any():
            # ids were < start, e.g. from an earlier type, or unknown (-1)
            raise ValueError("unknown IDs")

        try:
            return self._features[type_name][feature_ilocs, :]
        except IndexError:
            # some of the indices were too large (from a later type)
            raise ValueError("unknown IDs")

    def feature_info(self):
        """
        Returns:
             A dictionary of type_name to a tuple of an integer representing the size of the
             features of that type, and the dtype of the features.
        """
        return {
            type_name: (type_features.shape[1], type_features.dtype)
            for type_name, type_features in self._features.items()
        }


class FlatAdjacencyList:
    """
    Stores an adjacency list in one contiguous numpy array in a format similar
    to a ragged tensor (https://www.tensorflow.org/guide/ragged_tensor).
    """

    def __init__(self, flat_array, splits):
        self.splits = splits
        self.flat = flat_array

    def __getitem__(self, idx):
        if idx < 0:
            raise KeyError("node ilocs must be non-negative.")
        start = self.splits[idx]
        stop = self.splits[idx + 1]
        return self.flat[start:stop]

    def items(self):
        for idx in range(len(self.splits) - 1):
            yield (idx, self[idx])


def _get_splits(node_ilocs_list, number_of_nodes, dtype):
    neigh_counts = sum(
        np.bincount(ilocs, minlength=number_of_nodes) for ilocs in node_ilocs_list
    )
    splits = np.zeros(len(neigh_counts) + 1, dtype=dtype)
    splits[1:] = np.cumsum(neigh_counts, dtype=dtype)
    return splits


class EdgeData(ElementData):
    """
    Args:
        ids (sequence): the IDs of each element
        sources (numpy.ndarray): the ilocs of the source of each edge
        targets (numpy.ndarray): the ilocs of the target of each edge
        weight (numpy.ndarray): the weight of each edge
        type_starts (list of tuple of type name, int): the starting iloc of the edges of each type within ``shared``
        number_of_nodes (int): the total number of nodes in the graph
        node_data (NodeData): node data containing node types and IDs
    """

    def __init__(self, ids, sources, targets, weights, type_starts, node_data):
        super().__init__(ids, type_starts)

        for name, column in {
            "sources": sources,
            "targets": targets,
            "weights": weights,
        }.items():
            if not isinstance(column, np.ndarray):
                raise TypeError(
                    f"{name}: expected a NumPy ndarray, found {type(column).__name__}"
                )

            if len(column.shape) != 1:
                raise TypeError(
                    f"{name}: expected rank-1 array, found shape {column.shape}"
                )

            if len(column) != len(self._id_index):
                raise TypeError(
                    f"{name}: expected length {len(self._id_index)} to match IDs, found length {len(column)}"
                )

        self.sources = sources
        self.targets = targets
        self.weights = weights

        self.number_of_nodes = len(node_data)
        self.node_data = node_data

        self.dtype = np.min_scalar_type(len(self.sources))

        # These are lazily initialized, to only pay the (construction) time and memory cost when
        # actually using them
        self._edges_dict = self._edges_in_dict = self._edges_out_dict = None
        self._edges_dict_by_other_node_type = None
        self._edges_in_dict_by_other_node_type = None
        self._edges_out_dict_by_other_node_type = None

        # when there's no neighbors for something, an empty array should be returned; this uses a
        # tiny dtype to minimise unnecessary type promotion (e.g. if this is used with an int32
        # array, the result will still be int32).
        self._empty_ilocs = np.array([], dtype=np.uint8)

    def _combine_sources_and_targets(self):
        """
        Get the combined array of sources and targets. Self-loops are masked out with a sentinel

        Returns:
            A tuple of (combined, num_self_loops)
        """
        num_edges = len(self.targets)

        # sentinel masks out node_ilocs so must be the same type as node_ilocs node edge_ilocs
        sentinel = np.cast[np.min_scalar_type(self.number_of_nodes)](-1)
        self_loops = self.sources == self.targets
        num_self_loops = self_loops.sum()

        combined = np.concatenate([self.sources, self.targets])
        # mask out duplicates of self loops
        combined[num_edges:][self_loops] = sentinel

        return combined, num_self_loops

    def _init_directed_adj_lists(self):
        self._edges_in_dict, self._edges_out_dict = self._create_directed_adj_lists()

    def _create_directed_adj_lists(self):
        # record the edge ilocs of incoming and outgoing edges

        def _to_dir_adj_list(arr):
            splits = _get_splits([arr], self.number_of_nodes, self._id_index.dtype)
            flat = np.argsort(arr).astype(self._id_index.dtype, copy=False)
            return FlatAdjacencyList(flat, splits)

        return _to_dir_adj_list(self.targets), _to_dir_adj_list(self.sources)

    def _init_undirected_adj_lists(self):
        self._edges_dict = self._create_undirected_adj_lists()

    def _create_undirected_adj_lists(self):
        # record the edge ilocs of both-direction edges
        num_edges = len(self.targets)

        # the dtype of the edge_ilocs
        # the argsort results in integers in [0, 2 * num_edges),
        # so the dtype potentially needs to be slightly larger
        dtype = np.min_scalar_type(2 * len(self.sources))

        combined, num_self_loops = self._combine_sources_and_targets()

        flat_array = np.argsort(combined).astype(dtype, copy=False)

        # get targets without self loops inplace
        # sentinels are sorted to the end
        filtered_targets = combined[num_edges:]
        filtered_targets.sort()

        # remove the sentinels if there are any (the full array will be retained
        # forever; we're assume that there's self loops are a small fraction
        # of the total number of edges)
        if num_self_loops > 0:
            flat_array = flat_array[:-num_self_loops]
            filtered_targets = filtered_targets[:-num_self_loops]

        flat_array %= num_edges
        splits = _get_splits(
            [self.sources, filtered_targets], self.number_of_nodes, dtype
        )

        return FlatAdjacencyList(flat_array, splits)

    def _init_directed_adj_lists_by_other_node_type(self):
        (
            self._edges_in_dict_by_other_node_type,
            self._edges_out_dict_by_other_node_type,
        ) = self._create_directed_adj_lists_by_other_node_type()

    def _create_directed_adj_lists_by_other_node_type(self):
        # record the edge ilocs of incoming, outgoing and both-direction edges
        source_types = self.node_data.type_ilocs[self.sources]
        target_types = self.node_data.type_ilocs[self.targets]

        def _to_directed_adj_list(arr, other_node_types):
            index = {}

            # edge ilocs sorted in order of node iloc
            flat = np.argsort(arr).astype(self._id_index.dtype, copy=False)

            for other_node_type in np.unique(other_node_types):

                # filter node ilocs based on other node type
                arr_filtered = arr[other_node_types == other_node_type]
                splits = _get_splits(
                    [arr_filtered], self.number_of_nodes, self._id_index.dtype
                )

                # filter edge ilocs based on other node type
                flat_filtered = flat[other_node_types[flat] == other_node_type]

                index[other_node_type] = FlatAdjacencyList(flat_filtered, splits)

            return index

        return (
            _to_directed_adj_list(self.targets, source_types),
            _to_directed_adj_list(self.sources, target_types),
        )

    def _init_undirected_adj_lists_by_other_node_type(self):
        self._edges_dict_by_other_node_type = (
            self._create_undirected_adj_lists_by_other_node_type()
        )

    def _create_undirected_adj_lists_by_other_node_type(self):
        # record the edge ilocs of incoming, outgoing and both-direction edges
        index = {}
        source_types = self.node_data.type_ilocs[self.sources]
        target_types = self.node_data.type_ilocs[self.targets]

        num_edges = len(self.targets)

        # the dtype of the edge_ilocs
        # the argsort results in integers in [0, 2 * num_edges),
        # so the dtype potentially needs to be slightly larger
        dtype = np.min_scalar_type(2 * len(self.sources))

        combined, num_self_loops = self._combine_sources_and_targets()

        flat_array = np.argsort(combined).astype(dtype, copy=False)

        # get targets without self loops inplace
        # sentinels are sorted to the end
        filtered_targets = combined[num_edges:]
        filtered_targets_sort_order = np.argsort(filtered_targets).astype(
            dtype, copy=False
        )
        filtered_targets = filtered_targets[filtered_targets_sort_order]
        filtered_source_types = source_types[filtered_targets_sort_order]

        # remove the sentinels if there are any (the full array will be retained
        # forever; we're assume that there's self loops are a small fraction
        # of the total number of edges)
        if num_self_loops > 0:
            flat_array = flat_array[:-num_self_loops]
            filtered_targets = filtered_targets[:-num_self_loops]
            filtered_source_types = filtered_source_types[:-num_self_loops]

        other_node_types = np.concatenate([target_types, source_types])[flat_array]
        flat_array %= num_edges

        for other_node_type in np.unique(other_node_types):
            # filter node ilocs based on other node type
            sources_filtered = self.sources[target_types == other_node_type]
            targets_filtered = filtered_targets[
                filtered_source_types == other_node_type
            ]

            # count how often each node iloc occurs to calculate splits
            splits = _get_splits(
                [sources_filtered, targets_filtered], self.number_of_nodes, dtype,
            )

            # filter edge ilocs based on other node type
            flat_filtered = flat_array[other_node_types == other_node_type]

            index[other_node_type] = FlatAdjacencyList(flat_filtered, splits)

        return index

    def _adj_lookup(self, *, ins, outs):
        if ins and outs:
            if self._edges_dict is None:
                self._init_undirected_adj_lists()
            return self._edges_dict
        if ins:
            if self._edges_in_dict is None:
                self._init_directed_adj_lists()
            return self._edges_in_dict
        if outs:
            if self._edges_out_dict is None:
                self._init_directed_adj_lists()
            return self._edges_out_dict

        raise ValueError(
            "expected at least one of 'ins' or 'outs' to be True, found neither"
        )

    def _adj_lookup_by_other_node_type(self, *, ins, outs):
        if ins and outs:
            if self._edges_dict_by_other_node_type is None:
                self._init_undirected_adj_lists_by_other_node_type()
            return self._edges_dict_by_other_node_type
        if ins:
            if self._edges_in_dict_by_other_node_type is None:
                self._init_directed_adj_lists_by_other_node_type()
            return self._edges_in_dict_by_other_node_type
        if outs:
            if self._edges_out_dict_by_other_node_type is None:
                self._init_directed_adj_lists_by_other_node_type()
            return self._edges_out_dict_by_other_node_type

        raise ValueError(
            "expected at least one of 'ins' or 'outs' to be True, found neither"
        )

    def degrees(self, *, ins=True, outs=True):
        """
        Compute the degrees of every non-isolated node.

        Args:
            ins (bool): count incoming edges
            outs (bool): count outgoing edges

        Returns:
            The in-, out- or total (summed) degree of all non-isolated nodes as a numpy array (if
            ``ret`` is the return value, ``ret[i]`` is the degree of the node with iloc ``i``)
        """
        adj = self._adj_lookup(ins=ins, outs=outs)
        return defaultdict(int, ((key, len(value)) for key, value in adj.items()))

    def edge_ilocs(
        self, node_iloc, *, ins, outs, other_node_type_iloc=None
    ) -> np.ndarray:
        """
        Return the integer locations of the edges for the given node iloc

        Args:
            node_iloc: the iloc of the node


        Returns:
            The integer locations of the edges for the given node iloc.
        """

        if other_node_type_iloc is not None:
            return self._adj_lookup_by_other_node_type(ins=ins, outs=outs).get(
                other_node_type_iloc, defaultdict(list)
            )[node_iloc]
        else:
            return self._adj_lookup(ins=ins, outs=outs)[node_iloc]
