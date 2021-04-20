"""
Script for training block dynamics models for system identification.

This script outlines the basic structure of a training script
for system identification with deep state space models.
"""

"""
STEP 1
We begin by importing the necessary components from NeuroMANCER, 
as well as specifying some hyperparameters used to define the structure and 
optimization of our model:
"""
import torch
import torch.nn.functional as F
import slim  # custom weights with constraints and stucture
import psl   # package generating time series data by simulating ODEs
# Neuromancer imports
from neuromancer import arg                             # argument parser
from neuromancer.datasets import load_dataset           # dataset loader
from neuromancer.activations import activations         # custom activations
from neuromancer import blocks                          # basic neural blocks, i.e. MLP, RNN, ResNet
from neuromancer import estimators, dynamics            # structured neural models, such as state space model
from neuromancer.problem import Problem, Objective      # loss functions, constraints, constrained problem definition object
from neuromancer.trainer import Trainer                 # main trainer object
from neuromancer.simulators import OpenLoopSimulator    # simulator object for assessing model prerformance on task
from neuromancer.visuals import VisualizerOpen          # plotting
from neuromancer.loggers import BasicLogger, MLFlowLogger   # logging the results


"""
STEP 2

"""

# TODO: BETTER documentation - comment each part of the code refering to the paper
# TODO: higher level user interface hiding get model components
# TODO: system ID template for hands on - to be filled by the user
# TODO: add output keys to each component, BETTER documentations of keys

def get_model_components(args, dataset, estim_name="estim", dynamics_name="dynamics"):
    torch.manual_seed(args.seed)
    if not args.state_estimator == 'fully_observable':
        nx = dataset.dims["Y"][-1] * args.nx_hidden
    else:
        nx = dataset.dims["Y"][-1]
    print('dims', dataset.dims)
    print('nx', nx)
    activation = activations[args.activation]
    linmap = slim.maps[args.linear_map]
    linargs = {"sigma_min": args.sigma_min, "sigma_max": args.sigma_max}

    nonlinmap = {
        "linear": linmap,
        "mlp": blocks.MLP,
        "rnn": blocks.RNN,
        "pytorch_rnn": blocks.PytorchRNN,
        "residual_mlp": blocks.ResMLP,
    }[args.nonlinear_map]

    estimator = {
        "linear": estimators.LinearEstimator,
        "mlp": estimators.MLPEstimator,
        "rnn": estimators.RNNEstimator,
        "residual_mlp": estimators.ResMLPEstimator,
        "fully_observable": estimators.FullyObservable,
    }[args.state_estimator](
        {**dataset.dims, "x0": (nx,)},
        nsteps=args.nsteps,
        window_size=args.estimator_input_window,
        bias=args.bias,
        linear_map=linmap,
        nonlin=activation,
        hsizes=[nx] * args.n_layers,
        input_keys=["Yp"],
        linargs=linargs,
        name=estim_name,
    )

    dynamics_model = (
        dynamics.blackbox_model(
            {**dataset.dims, "x0_estim": (nx,)},
            linmap,
            nonlinmap,
            bias=args.bias,
            n_layers=args.n_layers,
            activation=activation,
            name=dynamics_name,
            input_keys={'x0': f'x0_{estimator.name}'},
            linargs=linargs
        ) if args.ssm_type == "blackbox"
        else dynamics.block_model(
            args.ssm_type,
            {**dataset.dims, "x0_estim": (nx,)},
            linmap,
            nonlinmap,
            bias=args.bias,
            n_layers=args.n_layers,
            activation=activation,
            name=dynamics_name,
            input_keys={'x0': f'x0_{estimator.name}'},
            linargs=linargs
        )
    )
    return estimator, dynamics_model


def get_objective_terms(args, dataset, estimator, dynamics_model):
    xmin = -0.2
    xmax = 1.2
    dxudmin = -0.05
    dxudmax = 0.05
    estimator_loss = Objective(
        [f"X_pred_{dynamics_model.name}", f"x0_{estimator.name}"],
        lambda X_pred, x0: F.mse_loss(X_pred[-1, :-1, :], x0[1:]),
        weight=args.Q_e,
        name="arrival_cost",
    )
    regularization = Objective(
        [f"reg_error_{estimator.name}", f"reg_error_{dynamics_model.name}"],
        lambda reg1, reg2: reg1 + reg2,
        weight=args.Q_sub,
        name="reg_error",
    )
    reference_loss = Objective(
        [f"Y_pred_{dynamics_model.name}", "Yf"], F.mse_loss, weight=args.Q_y, name="ref_loss"
    )
    state_smoothing = Objective(
        [f"X_pred_{dynamics_model.name}"],
        lambda x: F.mse_loss(x[1:], x[:-1]),
        weight=args.Q_dx,
        name="state_smoothing",
    )
    observation_lower_bound_penalty = Objective(
        [f"Y_pred_{dynamics_model.name}"],
        lambda x: torch.mean(F.relu(-x + xmin)),
        weight=args.Q_con_x,
        name="y_low_bound_error",
    )
    observation_upper_bound_penalty = Objective(
        [f"Y_pred_{dynamics_model.name}"],
        lambda x: torch.mean(F.relu(x - xmax)),
        weight=args.Q_con_x,
        name="y_up_bound_error",
    )

    objectives = [regularization, reference_loss, estimator_loss]
    constraints = [
        state_smoothing,
        observation_lower_bound_penalty,
        observation_upper_bound_penalty,
    ]

    if args.ssm_type != "blackbox":
        if "U" in dataset.data:
            inputs_max_influence_lb = Objective(
                [f"fU_{dynamics_model.name}"],
                lambda x: torch.mean(F.relu(-x + dxudmin)),
                weight=args.Q_con_fdu,
                name="input_influence_lb",
            )
            inputs_max_influence_ub = Objective(
                [f"fU_{dynamics_model.name}"],
                lambda x: torch.mean(F.relu(x - dxudmax)),
                weight=args.Q_con_fdu,
                name="input_influence_ub",
            )
            constraints += [inputs_max_influence_lb, inputs_max_influence_ub]
        if "D" in dataset.data:
            disturbances_max_influence_lb = Objective(
                [f"fD_{dynamics_model.name}"],
                lambda x: torch.mean(F.relu(-x + dxudmin)),
                weight=args.Q_con_fdu,
                name="dist_influence_lb",
            )
            disturbances_max_influence_ub = Objective(
                [f"fD_{dynamics_model.name}"],
                lambda x: torch.mean(F.relu(x - dxudmax)),
                weight=args.Q_con_fdu,
                name="dist_influence_ub",
            )
            constraints += [
                disturbances_max_influence_lb,
                disturbances_max_influence_ub,
            ]

    return objectives, constraints


if __name__ == "__main__":
    """
    STEP 2
    Dataset load and argument parser
    
    NeuroMANCER currently supports both static and emulated datasets 
    (generated from governing equations for various systems) via PSL. 
    Here, we load a dataset using simulated data from 'aero' system representing
    plane aerodynamics with 6DOF.
    
    for available systems in PSL library check: psl.systems.keys()
    for available datasets in PSL library check: psl.datasets.keys()
    """

    system = 'aero'         # keyword of selected system
    parser = arg.ArgParser(parents=[arg.log(), arg.opt(), arg.data(system=system),
                                    arg.loss(), arg.lin(), arg.ssm()])

    grp = parser.group('OPTIMIZATION')
    grp.add("-eval_metric", type=str, default="loop_dev_ref_loss",
            help="Metric for model selection and early stopping.")
    args, grps = parser.parse_arg_groups()
    print({k: str(getattr(args, k)) for k in vars(args) if getattr(args, k)})

    device = f"cuda:{args.gpu}" if args.gpu is not None else "cpu"

    log_constructor = MLFlowLogger if args.logger == 'mlflow' else BasicLogger
    metrics = ["nstep_dev_loss", "loop_dev_loss", "best_loop_dev_loss",
               "nstep_dev_ref_loss", "loop_dev_ref_loss"]
    logger = log_constructor(args=args, savedir=args.savedir, verbosity=args.verbosity, stdout=metrics)

    # # CUSTOM time series file following PSL dataset column naming convention # #
    # path = psl.datasets[system]
    # dataset = load_dataset(args, device, 'openloop', file_path=path, split_ratio=[40, 30, 30])
    dataset = load_dataset(args, device, 'openloop')
    print(dataset.dims)

    estimator, dynamics_model = get_model_components(args, dataset)
    objectives, constraints = get_objective_terms(args, dataset, estimator, dynamics_model)

    model = Problem(objectives, constraints, [estimator, dynamics_model])
    model = model.to(device)

    simulator = OpenLoopSimulator(model=model, dataset=dataset, eval_sim=not args.skip_eval_sim)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    trainer = Trainer(
        model,
        dataset,
        optimizer,
        logger=logger,
        simulator=simulator,
        epochs=args.epochs,
        eval_metric=args.eval_metric,
        patience=args.patience,
        warmup=args.warmup,
    )

    best_model = trainer.train()
    best_outputs = trainer.evaluate(best_model)

    visualizer = VisualizerOpen(
        dataset,
        dynamics_model,
        args.verbosity,
        args.savedir,
        training_visuals=False,
        trace_movie=False,
    )
    plots = visualizer.eval(best_outputs)

    logger.log_artifacts(plots)
    logger.clean_up()