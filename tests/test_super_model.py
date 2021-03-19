#####################################################
# Copyright (c) Xuanyi Dong [GitHub D-X-Y], 2021.03 #
#####################################################
# pytest ./tests/test_super_model.py -s             #
#####################################################
import sys, random
import unittest
import pytest
from pathlib import Path

lib_dir = (Path(__file__).parent / ".." / "lib").resolve()
print("library path: {:}".format(lib_dir))
if str(lib_dir) not in sys.path:
    sys.path.insert(0, str(lib_dir))

import torch
from xlayers import super_core
import spaces


class TestSuperLinear(unittest.TestCase):
    """Test the super linear."""

    def test_super_linear(self):
        out_features = spaces.Categorical(12, 24, 36)
        bias = spaces.Categorical(True, False)
        model = super_core.SuperLinear(10, out_features, bias=bias)
        print("The simple super linear module is:\n{:}".format(model))

        print(model.super_run_type)
        self.assertTrue(model.bias)

        inputs = torch.rand(32, 10)
        print("Input shape: {:}".format(inputs.shape))
        print("Weight shape: {:}".format(model._super_weight.shape))
        print("Bias shape: {:}".format(model._super_bias.shape))
        outputs = model(inputs)
        self.assertEqual(tuple(outputs.shape), (32, 36))

        abstract_space = model.abstract_search_space
        abstract_child = abstract_space.random()
        print("The abstract searc space:\n{:}".format(abstract_space))
        print("The abstract child program:\n{:}".format(abstract_child))

        model.set_super_run_type(super_core.SuperRunMode.Candidate)
        model.apply_candiate(abstract_child)

        output_shape = (32, abstract_child["_out_features"].value)
        outputs = model(inputs)
        self.assertEqual(tuple(outputs.shape), output_shape)

    def test_super_mlp(self):
        hidden_features = spaces.Categorical(12, 24, 36)
        out_features = spaces.Categorical(12, 24, 36)
        mlp = super_core.SuperMLP(10, hidden_features, out_features)
        print(mlp)
        self.assertTrue(mlp.fc1._out_features, mlp.fc2._in_features)

        abstract_space = mlp.abstract_search_space
        print("The abstract search space for SuperMLP is:\n{:}".format(abstract_space))
        self.assertEqual(
            abstract_space["fc1"]["_out_features"],
            abstract_space["fc2"]["_in_features"],
        )
        self.assertTrue(
            abstract_space["fc1"]["_out_features"]
            is abstract_space["fc2"]["_in_features"]
        )

        abstract_space.clean_last_sample()
        abstract_child = abstract_space.random(reuse_last=True)
        print("The abstract child program is:\n{:}".format(abstract_child))
        self.assertEqual(
            abstract_child["fc1"]["_out_features"].value,
            abstract_child["fc2"]["_in_features"].value,
        )
