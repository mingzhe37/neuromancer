"""

"""
from typing import Dict, List, Callable
from abc import ABC, abstractmethod
# pytorch imports
import torch
import torch.nn as nn
#local imports
import estimators
import policies
import dynamics
from blocks import MLP


class Objective(nn.Module):
    def __init__(self, variable_names: List[str], loss: Callable[..., torch.Tensor], weight=1.0):
        """

        :param variable_names: List of str
        :param loss: (callable) Number of arguments of the callable should equal the number of strings in variable names.
                                Arguments to callable should be torch.Tensor and return type a 0-dimensional torch.Tensor
        :param weight: (float) Weight of objective for calculating multi-objective loss function
        """
        self.variable_names = variable_names
        self.weight = weight
        self.loss = loss

    def __call__(self, variables: Dict[str, torch.Tensor]) -> torch.Tensor:
        """

        :param variables: (dict, {str: torch.Tensor}) Should contain keys corresponding to self.variable_names
        :return: 0-dimensional torch.Tensor that can be cast as a floating point number
        """
        return self.weight*self.loss(*[variables[k] for k in self.variable_names])


class Problem(nn.Module):

    def __init__(self, objectives: List[Objective],
                 components: List[Callable[[Dict[str, torch.Tensor]], Dict[str, torch.Tensor]]]):
        """
        This is similar in spirit to a nn.Sequential module. However,
        by concatenating input and output dictionaries for each component
        module we can represent arbitrary directed acyclic computation graphs.
        In addition the Problem module takes care of calculating weighted multi-objective
        loss functions via the list of Objective objects which calculate loss terms
        from aggregated input and set of outputs from the component modules.

        :param objectives: list of Objective objects
        :param components: list of Component objects
        """
        super().__init__()
        self.objectives = nn.ModuleList(objectives)
        self.components = nn.ModuleList(components)

    def _calculate_loss(self, variables: Dict[str, torch.Tensor]) -> torch.Tensor:
        loss = 0.0
        for variable_names, objective in self.objectives:
            loss += objective(variables)
        return loss

    def forward(self, data: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        output_dict = self.step(data)
        loss = self._calculate_loss(output_dict)
        output_dict = {'loss': loss, **output_dict}
        return {f'{data.name}_{k}': v for k, v in output_dict.items()}

    def step(self, input_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        output_dict = dict()
        for component in self.components:
            output_dict = component(input_dict)
            assert set(output_dict.keys()) - set(input_dict.keys()) == set(output_dict.keys())
            input_dict = {**input_dict, **output_dict}
        return output_dict



#
#
# class OpenLoop(Problem):
#     def __init__(self, constraints, model, estim):
#         """
#         :param constraints: list of Objective objects
#         :param model: SSM mappings, see dynamics.py
#         :param estim: state estimator mapping, see estimators.py
#
#         input data trajectories:
#         Y: measured outputs p (past)
#         U: control inputs p (past), f (future)
#         D: measured disturbances p (past), f (future)
#         nsamples: prediction horizon length
#         """
#         # TODO: lambda expressions on variables will not yeald to scalar values unless we do mean over samples
#         super().__init__(constraints)
#         self.objectives += [Objective(['xN_model', 'x0_estimator'], torch.nn.functional.mse_loss, weight=1.0),
#                             Objective(['reg_error_estim', 'reg_error_model'], lambda reg1, reg2: reg1 + reg2, weight=1.0),
#                             Objective(['Y_pred', 'Yf'], torch.nn.functional.mse_loss, weight=1.0),
#                             Objective(['X_pred'], lambda x: (x[1:] - x[:-1])*(x[1:] - x[:-1]))]
#         self.model = model
#         self.estim = estim
#
#     def step(self, data):
#         Yp, Yf, Up, Uf, Dp, Df, nsamples = data['Yp'], data['Yf'], data['Up'], data['Uf'], data['Dp'], data['Df'], data['Yf'].shape[0]
#         x0, reg_error_estim = self.estim(Yp, Up, Dp)
#         X_pred, Y_pred, reg_error_model = self.model(x=x0, U=Uf, D=Df, nsamples=nsamples)
#
#         return {'reg_error_estim': reg_error_estim,
#                 'reg_error_model': reg_error_model,
#                 'X_pred': X_pred,
#                 'Y_pred': Y_pred,
#                 'xN_model':  X_pred[-1, :-1, :],
#                 'x0_estimator': x0[1:]}
#
#
# class ClosedLoop(Problem):
#     def __init__(self, constraints, model, estim, policy, Q_estim=1.0, Q_reg=1.0, Q_policy=1.0, Q_model=1.0):
#         """
#         :param constraints: list of Objective objects
#         :param model: SSM mappings, see dynamics.py
#         :param estim: state estimator mapping, see estimators.py
#         :param policy: policy mapping, see policies.py
#         input data trajectories:
#         Y: measured outputs p (past)
#         U: control inputs  f (future)
#         D: measured disturbances p (past), f (future)
#         R: desired references f (future)
#         """
#         super().__init__(constraints)
#         # TODO: ['xN_model', 'x0_estimator'] - I am not sure about this constraint
#         # TODO: add Adaptive control mode
#         #  right now, this would work only for policy optimization  with fixed model params
#         #  ['Y_pred', 'Yf] this constraint should be optional, only in case of adaptive control,
#         #  for adaptive mode: need to instantiate a second model with shared weights to track the system ID loss
#         self.objectives += [Objective(['xN_model', 'x0_estimator'], torch.nn.functional.mse_loss, weight=1.0),
#                             Objective(['reg_error_estim', 'reg_error_model'], lambda reg1, reg2: reg1 + reg2,
#                                       weight=1.0),
#                             Objective(['Y_pred', 'Rf'], torch.nn.functional.mse_loss, weight=1.0),
#                             Objective(['X_pred'], lambda x: (x[1:] - x[:-1]) * (x[1:] - x[:-1]))]
#         self.model = model
#         self.estim = estim
#         self.policy = policy
#
    def step(self, data):
        # TODO: we want to have flexible policy definition with custom variables as arguments
        # for instance data can have additional parameters such as Xmin, Xmax, which we can use
        # We may have data dictionary with varying keys depending on application with only subset of mandatory keys such as Yp, Up
        # we want something like this:
            #  from types import SimpleNamespace
            #  d = {'a': 1, 'b': 2}
            #  n = SimpleNamespace(**d)
            #  n.a
        # and then we can concatenate the selected data for the policy
        # or we can hand the data dict with flags to all submodules and unpack them inside
        # where flags would indicate the use of the data in each submodule
        Yp, Yf, Up, Rf, Dp, Df, nsamples = data['Yp'], data['Yf'], data['Up'], data['Rf'], data['Dp'], data['Df'], data['Yf'].shape[0]
        x0, reg_error_estim = self.estim(Yp, Up, Dp)
        Uf, reg_error_policy = self.policy(x0, Df, Rf)
        Uf = Uf.unsqueeze(2).reshape(Uf.shape[0], self.model.nu, -1)
        Uf = Uf.permute(2, 0, 1)
        X_pred, Y_pred, reg_error_model = self.model(x=x0, U=Uf, D=Df, nsamples=nsamples)
        return {'reg_error_estim': reg_error_estim,
                'reg_error_model': reg_error_model,
                'reg_error_policy': reg_error_policy,
                'X_pred': X_pred,
                'Y_pred': Y_pred,
                'xN_model':  X_pred[-1, :-1, :],
                'x0_estimator': x0[1:],
                'Uf': Uf}
#
#
# class pOP(Problem):
#     def __init__(self, constraints, policy):
#         """
#         parametric optimization problem (pOP)
#              min_Theta objective(X,S,data)
#              s.t. S = constraints(X,data)
#                   X = policy_Theta(data)
#         :param constraints: list of Objective objects
#         :param policy: problem solution mapping X = f(Theta), see policies.py
#         input data:
#         whatever works with constraints
#         """
#         super().__init__(constraints)
#         self.objectives += []
#         self.policy = policy
#
#     def step(self, data):
#         # TODO: unwrapper of whatever is inside data
#         data1, nsamples = data['data1'], data['data1'].shape[0]
#         X, reg_error_policy = self.policy(data1)
#         return {'reg_error_policy': reg_error_policy,
#                 'X': X}
#
#
#
# if __name__ == '__main__':
#     nx, ny, nu, nd = 15, 7, 5, 3
#     Np = 2
#     Nf = 10
#     samples = 100
#     # Data format: (N,samples,dim)
#     x = torch.rand(samples, nx)
#     Yp = torch.rand(Np, samples, ny)
#     Up = torch.rand(Np, samples, nu)
#     Uf = torch.rand(Nf, samples, nu)
#     Dp = torch.rand(Np, samples, nd)
#     Df = torch.rand(Nf, samples, nd)
#     Rf = torch.rand(Nf, samples, ny)
#     x0 = torch.rand(samples, nx)
#
#     # block  SSM
#     fx, fu, fd = [MLP(insize, nx, hsizes=[64, 64, 64]) for insize in [nx, nu, nd]]
#     fy = MLP(nx, ny, hsizes=[64, 64, 64])
#     model1 = dynamics.BlockSSM(nx, nu, nd, ny, fx, fy, fu, fd)
#     model_out = model1(x0, Uf, Df)
#
#     # black box SSM
#     fxud = MLP(nx + nu + nd, nx, hsizes=[64, 64, 64])
#     model2 = dynamics.BlackSSM(nx, nu, nd, ny, fxud, fy)
#     model_out = model2(x0, Uf, Df)
#
#     # TODO: issue with the estimator switching 0th index with 1st index
#     est = estimators.LinearEstimator(ny, nx)
#     est_out = est(Yp, Up, Dp)
#
#     pol = policies.LinearPolicy(nx, nu, nd, ny, Nf)
#     pol_out = pol(x0, Df, Rf)
#
#     ol = OpenLoop([],model1,est)
#     ol_out = ol(Yp, Up, Uf, Dp, Df)
#
#     cl = ClosedLoop(model1, est, pol)
#     cl_out = cl(Yp, Up, Dp, Df, Rf)
#
#     ol = OpenLoop(model2, est)
#     ol_out = ol(Yp, Up, Uf, Dp, Df)
#
#     cl = ClosedLoop(model2, est, pol)
#     cl_out = cl(Yp, Up, Dp, Df, Rf)