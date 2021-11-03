"""
Solve Quadratic Programming (QP) problem using Neuromancer toolbox:
minimize     x^2+y^2
subject to   x+y-p >= 0

problem parameters:            p
problem decition variables:    x, y

Primal-dual solution with KKT conditions
"""

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
import slim
import matplotlib.pyplot as plt
import matplotlib.patheffects as patheffects
import cvxpy as cp
import numpy as np

from neuromancer.trainer import Trainer
from neuromancer.problem import Problem
import neuromancer.arg as arg
from neuromancer.constraint import Variable, Objective, Loss
from neuromancer.activations import activations
from neuromancer import policies
from neuromancer.loggers import BasicLogger
from neuromancer.dataset import normalize_data, split_static_data, StaticDataset
from neuromancer.plot import plot_loss_mpp, plot_solution_mpp


def arg_mpLP_problem(prefix=''):
    """
    Command line parser for mpLP problem definition arguments

    :param prefix: (str) Optional prefix for command line arguments to resolve naming conflicts when multiple parsers
                         are bundled as parents.
    :return: (arg.ArgParse) A command line parser
    """
    parser = arg.ArgParser(prefix=prefix, add_help=False)
    gp = parser.group("mpLP")
    gp.add("-Q", type=float, default=0.0,
           help="loss function weight.")  # tuned value: 1.0
    gp.add("-Q_sub", type=float, default=0.0,
           help="regularization weight.")
    gp.add("-Q_con_dummy", type=float, default=0.0001,
           help="dummy constraints penalty weight.")  # tuned value: 1.0
    gp.add("-Q_con", type=float, default=1.0,
           help="constraints penalty weight.")  # tuned value: 1.0
    gp.add("-Q_kkt", type=float, default=1.0,
           help="KKT constraints penalty weight.")  # tuned value: 1.0
    gp.add("-Q_kkt_stat", type=float, default=1.0,  # tuned value: 1.0
           help="KKT constraints penalty weight for stationarity condition.")  # tuned value: 2.0
    gp.add("-nx_hidden", type=int, default=80,
           help="Number of hidden states of the solution map")
    gp.add("-n_layers", type=int, default=2,
           help="Number of hidden layers of the solution map")
    gp.add("-bias", action="store_true",
           help="Whether to use bias in the neural network block component models.")
    gp.add("-data_seed", type=int, default=408,
           help="Random seed used for simulated data")
    gp.add("-epochs", type=int, default=400,
           help='Number of training epochs')
    gp.add("-lr", type=float, default=0.001,
           help="Step size for gradient descent.")
    gp.add("-patience", type=int, default=200,
           help="How many epochs to allow for no improvement in eval metric before early stopping.")
    gp.add("-warmup", type=int, default=200,
           help="Number of epochs to wait before enacting early stopping policy.")
    return parser


def get_dataloaders(data, norm_type=None, split_ratio=None, num_workers=0):
    """This will generate dataloaders for a given dictionary of data.
    Dataloaders are hard-coded for full-batch training to match NeuroMANCER's training setup.

    :param data: (dict str: np.array or list[dict str: np.array]) data dictionary or list of data
        dictionaries; if latter is provided, multi-sequence datasets are created and splits are
        computed over the number of sequences rather than their lengths.
    :param norm_type: (str) type of normalization; see function `normalize_data` for more info.
    :param split_ratio: (list float) percentage of data in train and development splits; see
        function `split_sequence_data` for more info.
    """

    if norm_type is not None:
        data, _ = normalize_data(data, norm_type)
    train_data, dev_data, test_data = split_static_data(data, split_ratio)

    train_data = StaticDataset(
        train_data,
        name="train",
    )
    dev_data = StaticDataset(
        dev_data,
        name="dev",
    )
    test_data = StaticDataset(
        test_data,
        name="test",
    )

    train_data = DataLoader(
        train_data,
        batch_size=len(train_data),
        shuffle=False,
        collate_fn=train_data.collate_fn,
        num_workers=num_workers,
    )
    dev_data = DataLoader(
        dev_data,
        batch_size=len(dev_data),
        shuffle=False,
        collate_fn=dev_data.collate_fn,
        num_workers=num_workers,
    )
    test_data = DataLoader(
        test_data,
        batch_size=len(test_data),
        shuffle=False,
        collate_fn=test_data.collate_fn,
        num_workers=num_workers,
    )

    return (train_data, dev_data, test_data), train_data.dataset.dims


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
    #  randomly sampled parameters theta generating superset of:
    #  theta_samples.min() <= theta <= theta_samples.max()
    np.random.seed(args.data_seed)
    nsim = 9000  # number of datapoints: increase sample density for more robust results
    samples = {"p1": np.random.uniform(low=1.0, high=11.0, size=(nsim, 1)),
               "p2": np.random.uniform(low=1.0, high=11.0, size=(nsim, 1))}
    nstep_data, dims = get_dataloaders(samples)
    train_data, dev_data, test_data = nstep_data

    """
    # # #  mpLP problem formulation in Neuromancer
    """
    n_var = 2           # number of primal decision variables
    # define primal solution map as MLP policy
    dims['U'] = (nsim, n_var)  # defining expected dimensions of the solution variable: internal policy key 'U'
    activation = activations['relu']
    linmap = slim.maps['linear']
    sol_map = policies.MLPPolicy(
        {**dims},
        bias=args.bias,
        linear_map=linmap,
        nonlin=activation,
        hsizes=[args.nx_hidden] * args.n_layers,
        input_keys=["p1", "p2"],
        name='primal_sol_map',
    )

    n_var = 4          # number of dual variables (nr. of constraints gradients)
    dims['U'] = (nsim, n_var)  # defining expected dimensions of the solution variable: internal policy key 'U'
    # define dual solution map
    dual_sol_map = policies.MLPPolicy(
        {**dims},
        bias=args.bias,
        linear_map=linmap,
        nonlin=activation,
        hsizes=[args.nx_hidden] * args.n_layers,
        input_keys=["p1", "p2"],
        name='dual_sol_map',
    )

    # variables
    x = Variable(f"U_pred_{sol_map.name}", name='x')[:, :, [0]]
    y = Variable(f"U_pred_{sol_map.name}", name='y')[:, :, [1]]
    xy = Variable(f"U_pred_{sol_map.name}", name='xy')
    mu = Variable(f"U_pred_{dual_sol_map.name}", name='mu')
    mu1 = Variable(f"U_pred_{dual_sol_map.name}", name='mu')[:, :, [0]]
    mu2 = Variable(f"U_pred_{dual_sol_map.name}", name='mu')[:, :, [1]]
    mu3 = Variable(f"U_pred_{dual_sol_map.name}", name='mu')[:, :, [2]]
    mu4 = Variable(f"U_pred_{dual_sol_map.name}", name='mu')[:, :, [3]]
    # sampled parameters
    p1 = Variable('p1')
    p2 = Variable('p2')

    # objective function
    f = x ** 2 + y ** 2
    obj = f.minimize(weight=args.Q, name='obj')
    # constraints
    g1 = -x - y + p1
    con_1 = (g1 <= 0)
    con_1.name = 'c1'
    g2 = x + y - p1 - 5
    con_2 = (g2 <= 0)
    con_2.name = 'c2'
    g3 = x - y + p2 - 5
    con_3 = (g3 <= 0)
    con_3.name = 'c3'
    g4 = -x + y - p2
    con_4 = (g4 <= 0)
    con_4.name = 'c4'

    # relaxed constraints for lagrangian
    con_1_r = args.Q_con_dummy*(g1 + 0.05 <= 0)
    con_1_r.name = 'c1_r'
    con_2_r = args.Q_con_dummy*(g2 + 0.05 <= 0)
    con_2_r.name = 'c2_r'
    con_3_r = args.Q_con_dummy*(g3 + 0.05 <= 0)
    con_3_r.name = 'c3_r'
    con_4_r = args.Q_con_dummy*(g4 + 0.05 <= 0)
    con_4_r.name = 'c4_r'

    # oposite constraints for comp. slack
    con_1_o = args.Q_con_dummy*(-1*g1 <= 0)
    con_1_o.name = 'c1_o'
    con_2_o = args.Q_con_dummy*(-1*g2 <= 0)
    con_2_o.name = 'c2_o'
    con_3_o = args.Q_con_dummy*(-1*g3 <= 0)
    con_3_o.name = 'c3_o'
    con_4_o = args.Q_con_dummy*(-1*g4 <= 0)
    con_4_o.name = 'c4_o'

    # create variables as proxies to constraints and objective
    l_var = Variable(obj.name, name='l_var')
    ineq_var1 = Variable(con_1.name, name='ineq_var1')
    ineq_var2 = Variable(con_2.name, name='ineq_var2')
    ineq_var3 = Variable(con_3.name, name='ineq_var3')
    ineq_var4 = Variable(con_4.name, name='ineq_var4')
    # oposite constraints variables
    ineq_var1_o = Variable(con_1_o.name, name='ineq_var1_o')
    ineq_var2_o = Variable(con_2_o.name, name='ineq_var2_o')
    ineq_var3_o = Variable(con_3_o.name, name='ineq_var3_o')
    ineq_var4_o = Variable(con_4_o.name, name='ineq_var4_o')
    # symbolic derivatives of objective and constraints penalties
    dloss_dxy = l_var.grad(xy)
    dcon_dxy1 = ineq_var1.grad(xy)
    dcon_dxy2 = ineq_var2.grad(xy)
    dcon_dxy3 = ineq_var3.grad(xy)
    dcon_dxy4 = ineq_var4.grad(xy)
    # symbolic derivatives of objective and constraints functions
    df_dxy = f.grad(xy)
    dg1_dxy = g1.grad(xy)
    dg2_dxy = g2.grad(xy)
    dg3_dxy = g3.grad(xy)
    dg4_dxy = g4.grad(xy)

    # TODO: OPTION 1 apply constraints correction via constraints penalties gradients: dcon_dxy1
    # https: // arxiv.org / pdf / 2104.12225.pdf
    # TODO: apply this correction on original constraints and on KKT
    # TODO: correction as a new component model taking values of decision variables
    #  and constraints gradients as inputs - apply on both equality and inequality?

    # TODO: OPTION 2 - implement offset free control principles
    # TODO: augment state space to track constraints violation errors in our model
    # TODO: all of these errors will be penalized to zero in an implicit gradient updates scheme

    # TODO: OPTION 3 tune weight factors of KKT
    # KKT conditions
    # L = f + mu1*g1 + mu2*g2 + mu3*g3 + mu4*g4   # Lagrangian
    L = f + mu1*ineq_var1 + mu2*ineq_var2 + mu3*ineq_var3 + mu4*ineq_var4   # Lagrangian
    L.name = 'Lagrangian'
    dL_dxy = L.grad(xy)                           # gradient of the lagrangian
    stat = args.Q_kkt_stat*(dL_dxy == 0)                               # stationarity condition
    # stat_dummy2 = 0*(dL_dxy == 0)                               # stationarity condition
    # dummy conditions for evaluating and checking gradients
    stat_dummy1 = 0 * (df_dxy + mu1 * dg1_dxy + mu2*dg2_dxy + mu3*dg3_dxy + mu3*dg4_dxy == 0)
    stat_dummy2 = 0 * (df_dxy + mu1*dcon_dxy1 + mu2*dcon_dxy2 + mu3*dcon_dxy3 + mu3*dcon_dxy4 == 0)
    # stat = args.Q_kkt_stat*(df_dxy + mu1*dg1_dxy + \
    #                    mu2*dg2_dxy + mu3*dg3_dxy + mu3*dg4_dxy == 0)     # stationarity via explicit gradients
    # stat = args.Q_kkt*(dloss_dxy + mu1*dcon_dxy1 + \
    #                    mu2*dcon_dxy2 + mu3*dcon_dxy3 + mu3*dcon_dxy3 == 0)     # stationarity via implicit gradients
    dual_feas = args.Q_kkt*(mu >= 0)                                # dual feasibility
    # comp_slack1 = args.Q_kkt*(mu1 * ineq_var1 == 0)                          # complementarity slackness
    # comp_slack2 = args.Q_kkt*(mu2 * ineq_var2 == 0)                          # complementarity slackness
    # comp_slack3 = args.Q_kkt*(mu3 * ineq_var3 == 0)                          # complementarity slackness
    # comp_slack4 = args.Q_kkt*(mu4 * ineq_var4 == 0)                          # complementarity slackness
    # comp_slack1 = args.Q_kkt*(mu1 * ineq_var1_o *(1/args.Q_con_dummy) == 0)                          # complementarity slackness
    # comp_slack2 = args.Q_kkt*(mu2 * ineq_var2_o *(1/args.Q_con_dummy) == 0)                          # complementarity slackness
    # comp_slack3 = args.Q_kkt*(mu3 * ineq_var3_o *(1/args.Q_con_dummy) == 0)                          # complementarity slackness
    # comp_slack4 = args.Q_kkt*(mu4 * ineq_var4_o *(1/args.Q_con_dummy) == 0)                          # complementarity slackness
    comp_slack1 = args.Q_kkt*(mu1 * g1 == 0)                          # complementarity slackness
    comp_slack2 = args.Q_kkt*(mu2 * g2 == 0)                          # complementarity slackness
    comp_slack3 = args.Q_kkt*(mu3 * g3 == 0)                          # complementarity slackness
    comp_slack4 = args.Q_kkt*(mu4 * g4 == 0)                          # complementarity slackness
    reg_dual = (mu == 0)^2                                # dual regularizer
    stat.name = 'stat'
    dual_feas.name = 'df'
    comp_slack1.name = 'cs1'
    comp_slack2.name = 'cs2'
    comp_slack3.name = 'cs3'
    comp_slack4.name = 'cs4'
    KKT = [stat, stat_dummy1, stat_dummy2, dual_feas,
           comp_slack1, comp_slack2, comp_slack3, comp_slack4]

    #  Second Order Sufficient Conditions (SOSC)
    # https://en.wikipedia.org/wiki/Karush%E2%80%93Kuhn%E2%80%93Tucker_conditions#Second-order_sufficient_conditions


    # TODO: create helper function in gradients
    #  to create KKT constraints from given objective and constraints and dual solution map

    # constrained optimization problem construction
    objectives = [obj]
    constraints = [args.Q_con*con_1, args.Q_con*con_2,
                   args.Q_con*con_3, args.Q_con*con_4,
                   # con_1_r, con_2_r,
                   # con_3_r, con_4_r,
                   # con_1_o, con_2_o,
                   # con_3_o, con_4_o,
                   ] + KKT
    components = [sol_map, dual_sol_map]
    model = Problem(objectives, constraints, components, grad_inference=True)

    """
    # # # Metrics and Logger
    """
    args.savedir = 'test_mpQP_1'
    args.verbosity = 1
    metrics = ["dev_loss", "dev_obj", "dev_stat", "dev_df",
               "dev_cs1", "dev_cs2", "dev_cs3", "dev_cs4",
               "dev_c1", "dev_c2", "dev_c3", "dev_c4"]
    logger = BasicLogger(args=args, savedir=args.savedir, verbosity=args.verbosity, stdout=metrics)
    logger.args.system = 'mpQP_1'

    """
    # # #  mpQP problem solution in Neuromancer
    """
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)

    # define trainer
    trainer = Trainer(
        model,
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


    """
    CVXPY benchmark
    """
    # Define and solve the CVXPY problem.
    x = cp.Variable(1)
    y = cp.Variable(1)
    p1 = 10.0  # problem parameter
    p2 = 10.0  # problem parameter
    def QP_param(p1, p2):
        prob = cp.Problem(cp.Minimize(x ** 2 + y ** 2),
                          [-x - y + p1 <= 0,
                           x + y - p1 - 5 <= 0,
                           x - y + p2 - 5 <= 0,
                           -x + y - p2 <= 0])
        return prob

    """
    Plots
    """
    # test problem parameters
    params = [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    p = 10.0
    x1 = np.arange(-1.0, 10.0, 0.05)
    y1 = np.arange(-1.0, 10.0, 0.05)
    xx, yy = np.meshgrid(x1, y1)
    fig, ax = plt.subplots(3,3)
    row_id = 0
    column_id = 0
    for i, p in enumerate(params):
        if i % 3 == 0 and i != 0:
            row_id += 1
            column_id = 0
        # eval objective and constraints
        J = xx ** 2 + yy ** 2
        c1 = xx + yy - p
        c2 = -xx - yy + p + 5
        c3 = -xx + yy - p + 5
        c4 = xx - yy + p

        # Plot
        cp_plot = ax[row_id,column_id].contourf(xx, yy, J, 50, alpha=0.4)
        ax[row_id,column_id].set_title(f'QP p={p}')
        cg1 = ax[row_id,column_id].contour(xx, yy, c1, [0], colors='mediumblue', alpha=0.7)
        cg2 = ax[row_id,column_id].contour(xx, yy, c2, [0], colors='mediumblue', alpha=0.7)
        cg3 = ax[row_id,column_id].contour(xx, yy, c3, [0], colors='mediumblue', alpha=0.7)
        cg4 = ax[row_id,column_id].contour(xx, yy, c4, [0], colors='mediumblue', alpha=0.7)
        plt.setp(cg1.collections,
                 path_effects=[patheffects.withTickedStroke()], alpha=0.7)
        plt.setp(cg2.collections,
                 path_effects=[patheffects.withTickedStroke()], alpha=0.7)
        plt.setp(cg3.collections,
                 path_effects=[patheffects.withTickedStroke()], alpha=0.7)
        plt.setp(cg4.collections,
                 path_effects=[patheffects.withTickedStroke()], alpha=0.7)
        fig.colorbar(cp_plot, ax=ax[row_id,column_id])

        # Solve QP
        prob = QP_param(p, p)
        prob.solve()

        # Solve DPP
        params = torch.tensor([p, p])
        xy_optim = model.components[0].net(params).detach().numpy()
        mu_optim = model.components[1].net(params).detach().numpy()
        datapoint = {}
        datapoint['p1'] = torch.tensor([[p]])
        datapoint['p2'] = torch.tensor([[p]])
        datapoint['name'] = "test"
        model_out = model(datapoint)

        print(f'primal solution QP x={x.value}, y={y.value}')
        print(f'parameter p={p, p}')
        print(f'primal solution DPP x1={xy_optim[0]}, x2={xy_optim[1]}')
        print(f'dual solution DPP mu={mu_optim}')
        print(f' L: {model_out["test_" + L.key]}')
        print(f' dL: {model_out["test_" + dL_dxy.key]}')
        print(f' f: {model_out["test_" + f.key]}')
        print(f' df: {model_out["test_" + df_dxy.key]}')
        print(f' stat: {model_out["test_" + stat.name]}')
        print(f' g1: {model_out["test_" + g1.key]}')
        print(f' g2: {model_out["test_" + g2.key]}')
        print(f' g3: {model_out["test_" + g3.key]}')
        print(f' g4: {model_out["test_" + g4.key]}')
        print(f' dg1: {model_out["test_" + dg1_dxy.key]}')
        print(f' dg2: {model_out["test_" + dg3_dxy.key]}')
        print(f' dg3: {model_out["test_" + dg3_dxy.key]}')
        print(f' dg4: {model_out["test_" + dg4_dxy.key]}')
        print(f' ineq1: {model_out["test_" + ineq_var1.key]}')
        print(f' ineq2: {model_out["test_" + ineq_var2.key]}')
        print(f' ineq3: {model_out["test_" + ineq_var3.key]}')
        print(f' ineq4: {model_out["test_" + ineq_var4.key]}')
        print(f' dineq1: {model_out["test_" + dcon_dxy1.key]}')
        print(f' dineq2: {model_out["test_" + dcon_dxy2.key]}')
        print(f' dineq3: {model_out["test_" + dcon_dxy3.key]}')
        print(f' dineq4: {model_out["test_" + dcon_dxy4.key]}')
        # print(f' ineq1_o: {model_out["test_" + ineq_var1_o.key]*(1/args.Q_con_dummy)}')
        # print(f' ineq2_o: {model_out["test_" + ineq_var2_o.key]*(1/args.Q_con_dummy)}')
        # print(f' ineq3_o: {model_out["test_" + ineq_var3_o.key]*(1/args.Q_con_dummy)}')
        # print(f' ineq4_o: {model_out["test_" + ineq_var4_o.key]*(1/args.Q_con_dummy)}')
        print(f' cs1: {model_out["test_" + comp_slack1.name]}')
        print(f' cs2: {model_out["test_" + comp_slack2.name]}')
        print(f' cs3: {model_out["test_" + comp_slack3.name]}')
        print(f' cs4: {model_out["test_" + comp_slack4.name]}')

        # Plot optimal solutions
        ax[row_id, column_id].plot(x.value, y.value, 'g*', markersize=10)
        ax[row_id,column_id].plot(xy_optim[0], xy_optim[1], 'r*', markersize=10)
        column_id +=1
    plt.show()
