from collections import OrderedDict, defaultdict

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

from metal.classifier import Classifier
from metal.end_model.em_defaults import em_default_config
from metal.end_model.loss import SoftCrossEntropyLoss
from metal.modules import IdentityModule
from metal.utils import (
    MetalDataset,
    hard_to_soft, 
    recursive_merge_dicts,
)

class EndModel(Classifier):
    """A dynamically constructed discriminative classifier

    Args:
        k: (int) the cardinality of the classifier
        input_module: (nn.Module) a module that converts the user-provided 
            model inputs to torch.Tensors. Defaults to IdentityModule.
        middle_modules: (nn.Module) a list of modules to execute between the
            input_module and task head. Defaults to nn.Linear.
        head_module: (nn.Module) a module to execute right before the final
            softmax that outputs a prediction for the task.
    """
    def __init__(self, k=2, input_module=None, middle_modules=None,
        head_module=None, **kwargs):
        config = recursive_merge_dicts(em_default_config, kwargs)
        super().__init__(k, config)

        self._build(input_module, middle_modules, head_module)

       # Show network
        if self.config['verbose']:
            print("\nNetwork architecture:")
            self._print()
            print()

    def _build(self, input_module, middle_modules, head_module):
        """
        TBD
        """
        input_layer = self._build_input_layer(input_module)
        middle_layers = self._build_middle_layers(middle_modules)
        head = self._build_task_head(head_module)  
        self.network = nn.Sequential(input_layer, *middle_layers, head)

        # Construct loss module
        self.criteria = SoftCrossEntropyLoss(reduce=True, size_average=False)

    def _build_input_layer(self, input_module):
        if input_module is None:
            input_module = IdentityModule()
        output_dim = self.config['layer_out_dims'][0]
        input_layer = self._make_layer(input_module, output_dim=output_dim)
        return input_layer

    def _build_middle_layers(self, middle_modules):
        middle_layers = nn.ModuleList()
        layer_out_dims = self.config['layer_out_dims']
        num_layers = len(layer_out_dims)
        for i in range(1, num_layers):
            if middle_modules is None:
                module = nn.Linear(*layer_out_dims[i-1:i+1])
                layer = self._make_layer(module, output_dim=layer_out_dims[i])
            else:
                module = middle_modules[i-1]
                layer = self._make_layer(module)
            middle_layers.add_module(f'layer{i}', layer)
        return middle_layers

    def _build_task_head(self, head_module):
        if head_module is None:
            head = nn.Linear(self.config['layer_out_dims'][-1], self.k)
        else:
            # Note that if head module is provided, it must have input dim of
            # the last middle module and output dim of self.k, the cardinality
            head = head_module        
        return head

    def _make_layer(self, module, output_dim=None):
        if isinstance(module, IdentityModule):
            return module
        layer = [module]
        layer.append(nn.ReLU())
        if self.config['batchnorm'] and output_dim:
            layer.append(nn.BatchNorm1d(output_dim))
        if self.config['dropout']:
            layer.append(nn.Dropout(self.config['dropout']))
        return nn.Sequential(*layer)

    def _print(self):
        print(self.network)

    def forward(self, x):
        """Returns a list of outputs for tasks 0,...t-1
        
        Args:
            x: a [batch_size, ...] batch from X
        """
        return self.network(x)

    @staticmethod
    def _reset_module(m):
        """A method for resetting the parameters of any module in the network

        First, handle special cases (unique initialization or none required)
        Next, use built in method if available
        Last, report that no initialization occured to avoid silent failure.

        This will be called on all children of m as well, so do not recurse
        manually.
        """
        if callable(getattr(m, 'reset_parameters', None)):
            m.reset_parameters()

    def update_config(self, update_dict):
        """Updates self.config with the values in a given update dictionary"""
        self.config = recursive_merge_dicts(self.config, update_dict)

    def _preprocess_Y(self, Y):
        """Convert Y to soft labels if necessary"""

        # If hard labels, convert to soft labels
        if Y.dim() == 1 or Y.shape[1] == 1:
            if not isinstance(Y, torch.LongTensor):
                self._check(Y, typ=torch.LongTensor)
            # FIXME: This could fail if last class was never predicted
            Y = hard_to_soft(Y, k=Y.max().long())
            # FIXME: This currently assumes that no model can output a 
            # prediction of 0 (i.e., if cardinality=5, Y[0] corresponds to
            # class 1 instead of 0, since the latter would give the model
            # a 5-dim output space but a 6-dim label space)
            Y = Y[:,1:]
        return Y

    def _make_data_loader(self, X, Y, data_loader_config):
        dataset = MetalDataset(X, self._preprocess_Y(Y))
        data_loader = DataLoader(dataset, shuffle=True, **data_loader_config)
        return data_loader

    def _get_loss_fn(self):
        loss_fn = lambda X, Y: self.criteria(self.forward(X), Y)
        return loss_fn

    def train(self, X_train, Y_train, X_dev=None, Y_dev=None, **kwargs):
        self.config = recursive_merge_dicts(self.config, kwargs)
        train_config = self.config['train_config']

        Y_train = self._to_torch(Y_train)
        Y_dev = self._to_torch(Y_dev)

        # Make data loaders
        loader_config = train_config['data_loader_config']
        train_loader = self._make_data_loader(X_train, Y_train, loader_config)

        # Initialize the model
        self.reset()

        # Create loss function
        loss_fn = self._get_loss_fn()

        # Execute training procedure
        self._train(train_loader, loss_fn, X_dev=X_dev, Y_dev=Y_dev)

    def predict_proba(self, X):
        """Returns a [n, k+1] tensor of soft (float) predictions."""
        return F.softmax(self.forward(X), dim=1).data.cpu().numpy()