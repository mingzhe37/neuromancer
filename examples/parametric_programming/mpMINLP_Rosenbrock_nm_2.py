"""
Solve the Rosenbrock problem, formulated as the MINLP using Neuromancer toolbox:
minimize     (1-x)^2 + a*(y-x^2)^2
subject to   (p/2)^2 <= x^2 + y^2 <= p^2
             x>=y

problem parameters:             a, p
problem decition variables:     x, y

https://en.wikipedia.org/wiki/Rosenbrock_function
"""


import numpy as np
import torch
from torch.utils.data import DataLoader
import slim
import matplotlib.pyplot as plt
import matplotlib.patheffects as patheffects
import cvxpy as cp
import numpy as np

from neuromancer.trainer import Trainer
from neuromancer.problem import Problem
import neuromancer.arg as arg
from neuromancer.constraint import Variable
from neuromancer.activations import activations
from neuromancer.loggers import BasicLogger, MLFlowLogger
from neuromancer.dataset import get_static_dataloaders
from neuromancer.loss import get_loss
from neuromancer.solvers import GradientProjection
from neuromancer.maps import Map
from neuromancer import blocks
from neuromancer.integers import IntegerProjection, IntegerInequalityProjection


def arg_mpLP_problem(prefix=''):
    """
    Command line parser for mpLP problem definition arguments

    :param prefix: (str) Optional prefix for command line arguments to resolve naming conflicts when multiple parsers
                         are bundled as parents.
    :return: (arg.ArgParse) A command line parser
    """
    parser = arg.ArgParser(prefix=prefix, add_help=False)
    gp = parser.group("mpLP")
    gp.add("-Q", type=float, default=1.0,
           help="loss function weight.")  # tuned value: 1.0
    gp.add("-Q_sub", type=float, default=0.0,
           help="regularization weight.")
    gp.add("-Q_con", type=float, default=100.0,
           help="constraints penalty weight.")  # tuned value: 1.0
    gp.add("-nx_hidden", type=int, default=80,
           help="Number of hidden states of the solution map")
    gp.add("-n_layers", type=int, default=4,
           help="Number of hidden layers of the solution map")
    gp.add("-bias", action="store_true",
           help="Whether to use bias in the neural network block component models.")
    gp.add("-data_seed", type=int, default=408,
           help="Random seed used for simulated data")
    gp.add("-epochs", type=int, default=1000,
           help='Number of training epochs')
    gp.add("-lr", type=float, default=0.001,
           help="Step size for gradient descent.")
    gp.add("-patience", type=int, default=100,
           help="How many epochs to allow for no improvement in eval metric before early stopping.")
    gp.add("-warmup", type=int, default=100,
           help="Number of epochs to wait before enacting early stopping policy.")
    gp.add("-loss", type=str, default='penalty',
           choices=['penalty', 'augmented_lagrange', 'barrier'],
           help="type of the loss function.")
    gp.add("-barrier_type", type=str, default='log10',
           choices=['log', 'log10', 'inverse'],
           help="type of the barrier function in the barrier loss.")
    gp.add("-eta", type=float, default=0.99,
           help="eta in augmented lagrangian.")
    gp.add("-sigma", type=float, default=2.0,
           help="sigma in augmented lagrangian.")
    gp.add("-mu_init", type=float, default=1.,
           help="mu_init in augmented lagrangian.")
    gp.add("-mu_max", type=float, default=1000.,
           help="mu_max in augmented lagrangian.")
    gp.add("-inner_loop", type=int, default=1,
           help="inner loop in augmented lagrangian")
    gp.add("-train_integer", default=True, choices=[True, False],
           help="Whether to use integer update during training or not.")
    gp.add("-inference_integer", default=False, choices=[True, False],
           help="Whether to use integer update during inference or not.")
    gp.add("-train_proj_int_ineq", default=False, choices=[True, False],
           help="Whether to use integer constraints projection during training or not.")
    gp.add("-inference_proj_int_ineq", default=True, choices=[True, False],
           help="Whether to use integer constraints projection during inference or not.")
    gp.add("-n_projections_train", type=int, default=1,
           help="number of mip constraints projection steps during training")
    gp.add("-n_projections_inference", type=int, default=10,
           help="number of mip constraints projections steps at the inference time")
    gp.add("-proj_dropout", type=float, default=0.5,
           help="random dropout of the mip constraints projections.")
    gp.add("-direction", default='gradient',
           choices=['gradient', 'random'],
           help="method for obtaining directions for integer constraints projections.")
    return parser


if __name__ == "__main__":
    """
    # # #  optimization problem hyperparameters
    """
    parser = arg.ArgParser(parents=[arg.log(),
                                    arg_mpLP_problem()])
    args, grps = parser.parse_arg_groups()
    args.bias = True
    device = f"cuda:{args.gpu}" if args.gpu is not None else "cpu"


    """
    # # #  Dataset 
    """
    #  randomly sampled parameters
    np.random.seed(args.data_seed)
    nsim = 20000  # number of datapoints: increase sample density for more robust results
    samples = {"a": np.random.uniform(low=0.2, high=2.2, size=(nsim, 1)),
               "p": np.random.uniform(low=0.0, high=6.0, size=(nsim, 1))}
    data, dims = get_static_dataloaders(samples)
    train_data, dev_data, test_data = data

    """
    # # #  mpNLP primal solution map architecture
    """
    func = blocks.MLP(insize=2, outsize=2,
                    bias=True,
                    linear_map=slim.maps['linear'],
                    nonlin=activations['relu'],
                    hsizes=[args.nx_hidden] * args.n_layers)
    sol_map = Map(func,
            input_keys=["a", "p"],
            output_keys=["x"],
            name='primal_map')

    """
    # # #  mpMINLP objective and constraints formulation in Neuromancer
    """
    # variables
    x = Variable("x")[:, [0]]
    y = Variable("x")[:, [1]]
    # sampled parameters
    p = Variable('p')
    a = Variable('a')

    # objective function
    f = (1-0.5*x)**2 + a*(0.5*y - 0.5*x**2)**2
    obj = f.minimize(weight=args.Q, name='obj')

    # constraints
    g1 = -x - y + p - 3
    con_1 = (g1 <= 0)
    con_1.name = 'c1'
    g2 = x + y - p - 2
    con_2 = (g2 <= 0)
    con_2.name = 'c2'
    g3 = x - y + p - 5
    con_3 = (g3 <= 0)
    con_3.name = 'c3'
    g4 = -x + y - p
    con_4 = (g4 <= 0)
    con_4.name = 'c4'

    # constrained optimization problem construction
    objectives = [obj]
    constraints = [args.Q_con*con_1, args.Q_con*con_2, args.Q_con*con_3, args.Q_con*con_4]
    components = [sol_map]

    if args.train_integer:  # MINLP = use integer correction update during training
        integer_map = IntegerProjection(input_keys=['x'],
                                        method='round_sawtooth',
                                        nsteps=1, stepsize=0.8,
                                        name='int_map')
        components.append(integer_map)
    if args.train_proj_int_ineq:
        int_projection = IntegerInequalityProjection(constraints, input_keys=["x"],
                                                     n_projections=args.n_projections_train,
                                                     dropout=args.proj_dropout,
                                                     direction=args.direction,
                                                     nsteps=3, stepsize=0.1, name='proj_int')
        components.append(int_projection)


    # create constrained optimization loss
    loss = get_loss(objectives, constraints, train_data, args)
    # construct constrained optimization problem
    problem = Problem(components, loss, grad_inference=args.train_proj_int_ineq)
    # plot computational graph
    problem.plot_graph()

    """
    # # # Metrics and Logger
    """
    args.savedir = 'test_mpMINLP_Rosebnrock'
    args.verbosity = 1
    metrics = ["train_loss", "train_obj", "train_mu_scaled_penalty_loss", "train_con_lagrangian",
               "train_mu", "train_c1", "train_c2", "train_c3"]
    if args.logger == 'stdout':
        Logger = BasicLogger
    elif args.logger == 'mlflow':
        Logger = MLFlowLogger
    logger = Logger(args=args, savedir=args.savedir, verbosity=args.verbosity, stdout=metrics)
    logger.args.system = 'mpmpMINLP_Rosebnrock'

    """
    # # #  mpQP problem solution in Neuromancer
    """
    optimizer = torch.optim.AdamW(problem.parameters(), lr=args.lr)

    # define trainer
    trainer = Trainer(
        problem,
        train_data,
        dev_data,
        test_data,
        optimizer,
        logger=logger,
        epochs=args.epochs,
        train_metric="train_loss",
        dev_metric="dev_loss",
        test_metric="test_loss",
        eval_metric="dev_loss",
        patience=args.patience,
        warmup=args.warmup,
        device=device,
    )

    # Train mpLP solution map
    best_model = trainer.train()
    best_outputs = trainer.test(best_model)
    # load best model dict
    problem.load_state_dict(best_model)

    """
    MIP Integer correction at inference
    """
    # integer projection to nearest integer
    int_map = IntegerProjection(input_keys=['x'],
                                   method='round_sawtooth',
                                   nsteps=1, stepsize=1.0,
                                   name='int_map')
    if args.inference_integer:
        if args.train_integer:
            problem.components[1] = int_map
        else:
            problem.components.append(int_map)

    # integer projection to feasible set
    int_projection = IntegerInequalityProjection(constraints, input_keys=["x"],  method="sawtooth",
                                                 n_projections=args.n_projections_inference,
                                                 dropout=args.proj_dropout,
                                                 direction=args.direction,
                                                 nsteps=1, stepsize=1.0, name='proj_int')
    if args.inference_proj_int_ineq:
        if args.train_proj_int_ineq:
            if args.train_integer:
                problem.components[2] = int_projection
            else:
                problem.components[1] = int_projection
        else:
            problem.components.append(int_projection)

    """
    Plots
    """
    # parameters
    a = 0.8
    p = 2.0

    plt.rc('axes', titlesize=14)  # fontsize of the title
    plt.rc('axes', labelsize=14)  # fontsize of the x and y labels
    plt.rc('xtick', labelsize=14)  # fontsize of the x tick labels
    plt.rc('ytick', labelsize=14)  # fontsize of the y tick labels

    x1 = np.arange(-5., 5., 0.02)
    y1 = np.arange(-5., 5., 0.02)
    xx, yy = np.meshgrid(x1, y1)

    # eval objective and constraints
    J = (1 - 0.5*xx) ** 2 + a * (0.5*yy - 0.5*xx ** 2) ** 2
    c1 = xx + yy - p + 3
    c2 = -xx - yy + p + 2
    c3 = -xx + yy - p + 5
    c4 = xx - yy + p

    levels = [0, 0.1, 0.5, 1.0, 2.0, 5.0,
             10., 20., 50., 100., 200.]
    fig, ax = plt.subplots(1, 1)
    cp = ax.contour(xx, yy, J, levels=levels, alpha=0.4, linewidths=2)
    cp = ax.contourf(xx, yy, J, levels=levels, alpha=0.4)

    fig.colorbar(cp)
    cg1 = ax.contour(xx, yy, c1, [0], colors='mediumblue', alpha=0.7)
    plt.setp(cg1.collections,
             path_effects=[patheffects.withTickedStroke()], alpha=0.7)
    cg2 = ax.contour(xx, yy, c2, [0], colors='mediumblue', alpha=0.7)
    plt.setp(cg2.collections,
             path_effects=[patheffects.withTickedStroke()], alpha=0.7)
    cg3 = ax.contour(xx, yy, c3, [0], colors='mediumblue', alpha=0.7)
    plt.setp(cg3.collections,
             path_effects=[patheffects.withTickedStroke()], alpha=0.7)
    cg4 = ax.contour(xx, yy, c4, [0], colors='mediumblue', alpha=0.7)
    plt.setp(cg4.collections,
             path_effects=[patheffects.withTickedStroke()], alpha=0.7)

    # Solution to mpMINLP via trained map
    datapoint = {}
    datapoint['a'] = torch.tensor([[a]])
    datapoint['p'] = torch.tensor([[p]])
    datapoint['name'] = "test"
    model_out = problem(datapoint)

    # intermediate solutions
    X = []
    Y = []
    if args.inference_proj_int_ineq:
        for k in range(args.n_projections_inference+2):
            x_nm_k = model_out['test_' + "x" + f'_{k}'][0, 0].detach().numpy()
            y_nm_k = model_out['test_' + "x" + f'_{k}'][0, 1].detach().numpy()
            X.append(x_nm_k)
            Y.append(y_nm_k)
            marker_size = 5 + k * (10 / args.n_projections_inference+2)
            ax.plot(x_nm_k, y_nm_k, 'g*', markersize=marker_size)
        ax.plot(np.asarray(X), np.asarray(Y), 'g--')

    # final solution
    x_nm = model_out['test_' + "x"][0, 0].detach().numpy()
    y_nm = model_out['test_' + "x"][0, 1].detach().numpy()
    print(x_nm)
    print(y_nm)
    ax.plot(x_nm, y_nm, 'r*', markersize=20)

    # Plot admissible integer solutions
    x_int = np.arange(-5., 6., 1.0)
    y_int = np.arange(-5., 6., 1.0)
    xx, yy = np.meshgrid(x_int, y_int)
    ax.plot(xx, yy, 'bo', markersize=3.5)
    ax.set_xlim(-5.0, 5.0)
    ax.set_ylim(-5.0, 5.0)
    fig.tight_layout()