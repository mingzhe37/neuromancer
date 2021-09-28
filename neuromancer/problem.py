"""

"""
# python base imports
from typing import Dict, List, Callable

# machine learning/data science imports
import torch
import torch.nn as nn

from neuromancer.constraint import Variable, Loss


class Problem(nn.Module):

    def __init__(self, objectives: List[Loss], constraints: List[Loss],
                 components: List[Callable[[Dict[str, torch.Tensor]], Dict[str, torch.Tensor]]]):
        """
        This is similar in spirit to a nn.Sequential module. However,
        by concatenating input and output dictionaries for each component
        module we can represent arbitrary directed acyclic computation graphs.
        In addition the Problem module takes care of calculating weighted multi-objective
        loss functions via the lists of Loss objects (constraints and objectives) which calculate loss terms
        from aggregated input and set of outputs from the component modules.

        :param objectives: list of objects which implement the Loss interface (e.g. Objective, Loss, or Constraint)
        :param constraints: list of objects which implement the Loss interface (e.g. Objective, Loss, or Constraint)
        :param components: list of objects which implement the component interface (e.g. Function, Policy, Estimator)
        """
        super().__init__()
        self.objectives = nn.ModuleList(objectives)
        self.constraints = nn.ModuleList(constraints)
        self.components = nn.ModuleList(components)
        self._check_unique_names()

    def _check_unique_names(self):
        num_unique = len(set([o.name for o in self.objectives] + [c.name for c in self.constraints]))
        num_objectives = len(self.objectives) + len(self.constraints)
        assert num_unique == num_objectives, "All objectives and constraints must have unique names."

    def _check_name_collision_dicts(self, input_dict: Dict[str, torch.Tensor],
                                  output_dict: Dict[str, torch.Tensor]):
        assert set(output_dict.keys()) - set(input_dict.keys()) == set(output_dict.keys()), \
            f'Name collision in input and output dictionaries, Input_keys: {input_dict.keys()},' \
            f'Output_keys: {output_dict.keys()}'

    def calculate_loss(self, input_dict: Dict[str, torch.Tensor]) -> torch.Tensor:
        """

        """
        # TODO: check this change!
        # TODO: now all losses and constraints return dict -
        #  this syntax will allow us to create variables as proxies to constraints and objectives -
        #  this in turn will allow to construct gradients and algebra on losses
        loss = 0.0
        for objective in self.objectives:
            output_dict = objective(input_dict)
            if isinstance(output_dict, torch.Tensor):
                output_dict = {objective.name: output_dict}
            self._check_name_collision_dicts(input_dict, output_dict)
            input_dict = {**input_dict, **output_dict}
            loss += output_dict[objective.name]
        for constraint in self.constraints:
            output_dict = constraint(input_dict)
            if isinstance(output_dict, torch.Tensor):
                output_dict = {constraint.name: output_dict}
            self._check_name_collision_dicts(input_dict, output_dict)
            input_dict = {**input_dict, **output_dict}
            loss += output_dict[constraint.name]
        input_dict['loss'] = loss
        return input_dict

    def forward(self, data: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        output_dict = self.step(data)
        output_dict = self.calculate_loss(output_dict)
        return {f'{data["name"]}_{k}': v for k, v in output_dict.items()}

    def step(self, input_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        for component in self.components:
            output_dict = component(input_dict)
            if isinstance(output_dict, torch.Tensor):
                output_dict = {component.name: output_dict}
            self._check_name_collision_dicts(input_dict, output_dict)
            input_dict = {**input_dict, **output_dict}
        return input_dict

    def compute_KKT(self, input_dict):
        """
        computing KKT conditions of the problem by using autodiff
        https://en.wikipedia.org/wiki/Karush%E2%80%93Kuhn%E2%80%93Tucker_conditions

        how it should work:
            1, we need dual network predicting dual variables as extra component before KKT eval
            2, KKT constraints need to be included in the forward pass
            3, penalties on KKT violations as additional loss term

        should it be method on in the problem class
        or standalone class taking constrants and objectives as arguments
        and creating new constraints corresponding to the KKT conditions
        and taking these new objects as additional constraints because that's what they are

        :return:
        """
        pass
    #     TODO: how to implement gradients of functions w.r.t. inputs?
    #      should we go component level or here?

        # TODO example
        var = self.objectives[0].var
        con = self.constraints[0]
        loss = self.objectives[0]
        key = var.key
        # TODO: slicing seems to overwrite the variable key
        # TODO: for each loss and constraints term include keys to be able to pull out the data from ditct
        # TODO: compute gradients of all constraints and losses
        var_grad = torch.autograd.grad(var(input_dict)[:, 0], input_dict[key])
        con_grad = torch.autograd.grad(con(input_dict), input_dict[key])
        loss_grad = torch.autograd.grad(loss(input_dict), input_dict[key])
    #     TODO: 1, using use_dual argument: after problem init instantiate new internal model (dual net) for learning dual variables?
    #     TODO: 2, OR in components include dual solution network whose outputs are dual variables


    def __repr__(self):
        s = "### MODEL SUMMARY ###\n\nCOMPONENTS:"
        if len(self.components) > 0:
            for c in self.components:
                s += f"\n  {repr(c)}"
            s += "\n"
        else:
            s += " none\n"

        s += "\nCONSTRAINTS:"
        if len(self.constraints) > 0:
            for c in self.constraints:
                s += f"\n  {repr(c)}"
            s += "\n"
        else:
            s += " none\n"

        s += "\nOBJECTIVES:"
        if len(self.objectives) > 0:
            for c in self.objectives:
                s += f"\n  {repr(c)}"
            s += "\n"
        else:
            s += " none\n"

        return s


class MSELoss(Loss):
    def __init__(self, variable_names, weight=1.0, name="mse_loss"):
        super().__init__(
            variable_names,
            nn.functional.mse_loss,
            weight=weight,
            name=name
        )


class RegularizationLoss(Loss):
    def __init__(self, variable_names, weight=1.0, name="reg_loss"):
        super().__init__(
            variable_names,
            lambda *x: torch.sum(*x),
            weight=weight,
            name=name
        )


if __name__ == '__main__':
    nx, ny, nu, nd = 15, 7, 5, 3
    Np = 2
    Nf = 10
    samples = 100
    # Data format: (N,samples,dim)
    x = torch.rand(samples, nx)
    Yp = torch.rand(Np, samples, ny)
    Up = torch.rand(Np, samples, nu)
    Uf = torch.rand(Nf, samples, nu)
    Dp = torch.rand(Np, samples, nd)
    Df = torch.rand(Nf, samples, nd)
    Rf = torch.rand(Nf, samples, ny)
    x0 = torch.rand(samples, nx)


