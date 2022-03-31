"""
Stochastic Differentiable predictive control (SDPC)

Learning to control 3D linear quadcopter model subject to additive uncertainites

based on MPC example from:
https://osqp.org/docs/examples/mpc.html
"""

import torch
import torch.nn.functional as F
import slim
import seaborn as sns
DENSITY_PALETTE = sns.color_palette("crest_r", as_cmap=True)
DENSITY_FACECLR = DENSITY_PALETTE(0.01)
sns.set_theme(style="white")
from pylab import *
import numpy as np
import matplotlib.pyplot as plt
import time

from neuromancer.activations import activations
from neuromancer import blocks, estimators, dynamics
from neuromancer.trainer import Trainer
from neuromancer.problem import Problem
from neuromancer.constraint import Loss
from neuromancer import policies
import neuromancer.arg as arg
from neuromancer.dataset import normalize_data, split_sequence_data, SequenceDataset
from torch.utils.data import DataLoader
from neuromancer.loggers import BasicLogger
from neuromancer.constraint import Variable
from neuromancer.loss import PenaltyLoss, BarrierLoss


def arg_dpc_problem(prefix=''):
    """
    Command line parser for DPC problem definition arguments

    :param prefix: (str) Optional prefix for command line arguments to resolve naming conflicts when multiple parsers
                         are bundled as parents.
    :return: (arg.ArgParse) A command line parser
    """
    parser = arg.ArgParser(prefix=prefix, add_help=False)
    gp = parser.group("DPC")
    gp.add("-nsteps", type=int, default=10,
           help="prediction horizon.")
    gp.add("-nx_hidden", type=int, default=100,
           help="Number of hidden states")
    gp.add("-n_layers", type=int, default=2,
           help="Number of hidden layers")
    gp.add("-bias", action="store_true",
           help="Whether to use bias in the neural network block component models.")
    gp.add("-epochs", type=int, default=2000,
           help='Number of training epochs')
    gp.add("-lr", type=float, default=0.001,
           help="Step size for gradient descent.")
    gp.add("-patience", type=int, default=100,
           help="How many epochs to allow for no improvement in eval metric before early stopping.")
    gp.add("-warmup", type=int, default=100,
           help="Number of epochs to wait before enacting early stopping policy.")
    gp.add("-validate", default=False, choices=[True, False],
           help="whether to validate stochastic guarantees or not.")
    gp.add("-loss", type=str, default='penalty',
           choices=['penalty', 'barrier'],
           help="type of the loss function.")
    gp.add("-barrier_type", type=str, default='log10',
           choices=['log', 'log10', 'inverse'],
           help="type of the barrier function in the barrier loss.")
    gp.add("-batch_second", default=True, choices=[True, False],
           help="whether the batch is a second dimension in the dataset.")
    return parser


def get_sequence_dataloaders(
    data, nsteps, moving_horizon=False, norm_type=None, split_ratio=None, num_workers=0,
):
    """This will generate dataloaders and open-loop sequence dictionaries for a given dictionary of
    data. Dataloaders are hard-coded for full-batch training to match NeuroMANCER's original
    training setup.

    :param data: (dict str: np.array or list[dict str: np.array]) data dictionary or list of data
        dictionaries; if latter is provided, multi-sequence datasets are created and splits are
        computed over the number of sequences rather than their lengths.
    :param nsteps: (int) length of windowed subsequences for N-step training.
    :param moving_horizon: (bool) whether to use moving horizon batching.
    :param norm_type: (str) type of normalization; see function `normalize_data` for more info.
    :param split_ratio: (list float) percentage of data in train and development splits; see
        function `split_sequence_data` for more info.
    """

    if norm_type is not None:
        data, _ = normalize_data(data, norm_type)
    train_data, dev_data, test_data = split_sequence_data(data, nsteps, moving_horizon, split_ratio)

    train_data = SequenceDataset(
        train_data,
        nsteps=nsteps,
        moving_horizon=moving_horizon,
        name="train",
    )
    dev_data = SequenceDataset(
        dev_data,
        nsteps=nsteps,
        moving_horizon=moving_horizon,
        name="dev",
    )
    test_data = SequenceDataset(
        test_data,
        nsteps=nsteps,
        moving_horizon=moving_horizon,
        name="test",
    )

    train_loop = train_data.get_full_sequence()
    dev_loop = dev_data.get_full_sequence()
    test_loop = test_data.get_full_sequence()

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

    return (train_data, dev_data, test_data), (train_loop, dev_loop, test_loop), train_data.dataset.dims


def get_loss(objectives, constraints, args):
    if args.loss == 'penalty':
        loss = PenaltyLoss(objectives, constraints, batch_second=args.batch_second)
    elif args.loss == 'barrier':
        loss = BarrierLoss(objectives, constraints, barrier=args.barrier_type,
                           batch_second=args.batch_second)
    return loss


def cl_simulate(A, B, C, policy, nstep=50, x0=None, w_sigma=0.1,
                xmin=-10, xmax=1, umin=None, umax=None, xmin_f=-1, xmax_f=1,
                ref=None, save_path=None):
    """

    :param A:
    :param B:
    :param net:
    :param nstep:
    :param x0:
    :return:
    """
    if x0 is None:
        x0 = np.zeros([A.shape[0],1])
    X_trajs = []
    U_trajs = []
    times = []
    I_s = []        # stability indicator
    I_c = []        # constraints indicator
    X_c_mean = []
    U_c_mean = []

    w_runs = 20
    for j in range(w_runs):
        x = x0
        X = [x]
        U = []
        Y = []
        for k in range(nstep+1):
            wf = w_sigma * np.random.randn(A.shape[0], 1)  # additive uncertainty
            x_torch = torch.tensor(x).float().transpose(0, 1)
            # taking a first control action based on RHC principle
            start_time = time.time()
            uout = policy({'x0_estimator': x_torch})
            sol_time = time.time() - start_time
            times.append(sol_time)
            u = uout['U_pred_policy'][0,:,:].detach().numpy().transpose()
            u = np.clip(u, umin.reshape(u.shape[0],1), umax.reshape(u.shape[0],1))
            # closed loop dynamics
            x = np.matmul(A, x) + np.matmul(B, u) + wf
            y = np.matmul(C, x)
            X.append(x)
            U.append(u)
            Y.append(y)
        Xnp = np.asarray(X)[:, :, 0]
        Unp = np.asarray(U)[:, :, 0]
        Ynp = np.asarray(Y)[:, :, 0]
        X_trajs.append(Xnp)
        U_trajs.append(Unp)

    mean_sol_time = np.mean(times)
    max_sol_time = np.max(times)
    print(f'mean sol time {mean_sol_time}')
    print(f'max sol time {max_sol_time}')

    if ref is None:
        ref = np.zeros(Ynp.shape)
    else:
        ref = ref[0:Ynp.shape[0], :]

    fig, ax = plt.subplots(2, 1)
    for j in range(w_runs):
        ax[0].plot(X_trajs[j], linewidth=1.0)
    # ax[0].plot(Xnp, label='x', linewidth=2)
    ax[0].plot(ref, 'k--', label='r', linewidth=2)
    ax[0].set(ylabel='$x$')
    ax[0].set(xlabel='time')
    ax[0].grid()
    ax[0].set_xlim(0, nstep)
    for j in range(w_runs):
        ax[1].plot(U_trajs[j], drawstyle='steps',  linewidth=1.0)
    # ax[1].plot(Unp, label='u', drawstyle='steps',  linewidth=2)
    ax[1].set(ylabel='$u$')
    if umin is not None:
        u_min = umin * np.ones([nstep + 1, umin.shape[0]])
        ax[1].plot(u_min, 'k--', linewidth=2)
    if umax is not None:
        u_max = umax * np.ones([nstep + 1, umax.shape[0]])
        ax[1].plot(u_max, 'k--', linewidth=2)
    ax[1].set(xlabel='time')
    ax[1].grid()
    ax[1].set_xlim(0, nstep)
    plt.tight_layout()
    if save_path is not None:
        plt.savefig(save_path+'/closed_loop_quadcopter_dpc.pdf')


def cl_validate(A, B, C, policy, nstep=50, x0=None, w_sigma=0.1,
                xmin=-10, xmax=10,  umin=-1, umax=2.5, xmin_f=-1, xmax_f=1,
                n_sim_p=100, n_sim_w=10, epsilon=0.1, delta=0.99):
    """

    :param A:
    :param B:
    :param net:
    :param nstep:
    :param x0:
    :return:
    """


    X_trajs = []
    U_trajs = []
    times = []
    I_s = []        # stability indicator
    I_c = []        # constraints indicator
    X_c_mean = []
    U_c_mean = []

    umin = umin.reshape(B.shape[1],1)
    umax = umax.reshape(B.shape[1],1)

    samples = n_sim_w*n_sim_p
    for j in range(samples):
        if x0 is None:
            # x0 = np.zeros([A.shape[0],1])
            X_dist_bound=0.1
            x0 = np.random.uniform(low=-X_dist_bound, high=X_dist_bound, size=(A.shape[0],1))
        x = x0
        X = [x]
        U = []
        Y = []
        for k in range(nstep+1):
            wf = w_sigma * np.random.randn(A.shape[0], 1)  # additive uncertainty
            x_torch = torch.tensor(x).float().transpose(0, 1)
            # taking a first control action based on RHC principle
            start_time = time.time()
            uout = policy({'x0_estimator': x_torch})
            sol_time = time.time() - start_time
            times.append(sol_time)
            u = uout['U_pred_policy'][0,:,:].detach().numpy().transpose()
            u = np.clip(u, umin, umax)
            # closed loop dynamics
            x = np.matmul(A, x) + np.matmul(B, u) + wf
            y = np.matmul(C, x)
            X.append(x)
            U.append(u)
            Y.append(y)
        Xnp = np.asarray(X)[:, :, 0]
        Unp = np.asarray(U)[:, :, 0]
        X_trajs.append(Xnp)
        U_trajs.append(Unp)

        # terminal constraint satisfaction for stability
        stab_idx = [3,4,5,9,10,11]
        stability_flag = int((x[stab_idx] > xmin_f).all() and (x[stab_idx] < xmax_f).all())
        I_s.append(stability_flag)

        # state and input constraints mean violations
        x_violations = np.mean(np.maximum(Xnp - xmax, 0) + np.maximum(-Xnp + xmin, 0))
        u_violations = np.mean(np.maximum(Unp - 2.5, 0) + np.maximum(-Unp -1, 0))
        X_c_mean.append(x_violations)
        U_c_mean.append(u_violations)

        # state and input constraints satisfaction
        x_con_flag = int((Xnp > xmin).all() and (Xnp < xmax).all())
        u_con_flag = int((Unp > -1).all() and (Unp < 2.5).all())
        constraints_flag = x_con_flag and u_con_flag
        I_c.append(constraints_flag)

    # evaluate empirical risk
    mu_tilde = np.mean(0.5*np.asarray(I_s) + 0.5*np.asarray(I_c))
    # evaluate conficence level given risk tolerance epsilon
    confidence = 1 - 2 * np.exp(-2 * samples * epsilon ** 2)
    mu = mu_tilde - epsilon
    # evaluate risk lower bound given number of samples, and desired confidence
    mu_lbound = mu_tilde - np.sqrt(-np.log(delta / 2) / (2 * samples))

    print(f'empirical risk {mu_tilde}')
    print(f'risk lower bound beta {mu_lbound}')
    print(f'choosen condifence delta {delta}')

    return mu, mu_tilde, confidence, mu_lbound, \
               I_s, I_c, X_c_mean, U_c_mean


if __name__ == "__main__":

    """
    # # #  Arguments
    """
    parser = arg.ArgParser(parents=[arg.log(),
                                    arg_dpc_problem()])
    args, grps = parser.parse_arg_groups()
    args.bias = True

    """
    # # # 3D quadcopter model 
    """

    # Discrete time model of a quadcopter
    A = np.array([
        [1., 0., 0., 0., 0., 0., 0.1, 0., 0., 0., 0., 0.],
        [0., 1., 0., 0., 0., 0., 0., 0.1, 0., 0., 0., 0.],
        [0., 0., 1., 0., 0., 0., 0., 0., 0.1, 0., 0., 0.],
        [0.0488, 0., 0., 1., 0., 0., 0.0016, 0., 0., 0.0992, 0., 0.],
        [0., -0.0488, 0., 0., 1., 0., 0., -0.0016, 0., 0., 0.0992, 0.],
        [0., 0., 0., 0., 0., 1., 0., 0., 0., 0., 0., 0.0992],
        [0., 0., 0., 0., 0., 0., 1., 0., 0., 0., 0., 0.],
        [0., 0., 0., 0., 0., 0., 0., 1., 0., 0., 0., 0.],
        [0., 0., 0., 0., 0., 0., 0., 0., 1., 0., 0., 0.],
        [0.9734, 0., 0., 0., 0., 0., 0.0488, 0., 0., 0.9846, 0., 0.],
        [0., -0.9734, 0., 0., 0., 0., 0., -0.0488, 0., 0., 0.9846, 0.],
        [0., 0., 0., 0., 0., 0., 0., 0., 0., 0., 0., 0.9846]
    ])
    B = np.array([
        [0., -0.0726, 0., 0.0726],
        [-0.0726, 0., 0.0726, 0.],
        [-0.0152, 0.0152, -0.0152, 0.0152],
        [-0., -0.0006, -0., 0.0006],
        [0.0006, 0., -0.0006, 0.0000],
        [0.0106, 0.0106, 0.0106, 0.0106],
        [0, -1.4512, 0., 1.4512],
        [-1.4512, 0., 1.4512, 0.],
        [-0.3049, 0.3049, -0.3049, 0.3049],
        [-0., -0.0236, 0., 0.0236],
        [0.0236, 0., -0.0236, 0.],
        [0.2107, 0.2107, 0.2107, 0.2107]])
    [nx, nu] = B.shape
    C = np.eye(nx,nx)

    # Constraints
    u0 = 10.5916
    umin_b = np.array([9.6, 9.6, 9.6, 9.6]) - u0
    umax_b = np.array([13., 13., 13., 13.]) - u0
    xmin_b = np.array([-np.pi / 6, -np.pi / 6, -10., -10., -10., -1.,
                     -10., -10., -10., -10., -10., -10.])
    xmax_b = np.array([np.pi / 6, np.pi / 6, 10., 10., 10., 10.,
                     10., 10., 10., 10., 10., 10.])
    # xmin_b = np.array([-np.pi / 6, -np.pi / 6, -np.inf, -np.inf, -np.inf, -1.,
    #                  -np.inf, -np.inf, -np.inf, -np.inf, -np.inf, -np.inf])
    # xmax_b = np.array([np.pi / 6, np.pi / 6, np.inf, np.inf, np.inf, np.inf,
    #                  np.inf, np.inf, np.inf, np.inf, np.inf, np.inf])

    # Initial and reference states
    x0 = np.zeros(12)
    xr = np.array([0., 0., 1., 0., 0., 0., 0., 0., 0., 0., 0., 0.])

    # number of datapoints
    n_sim_p = 10000  # number of parametric samples
    n_sim_w = 10  # number of disturbance samples per parameter
    nsim = n_sim_p * n_sim_w  # rule of thumb: more data samples -> improved control performance
    # uncertainties
    w_sigma = 0.02


    """
    # # #  Dataset 
    """

    #  randomly sampled input trajectories for training
    #  we treat states as observables, i.e. Y = X
    X_dist_bound = 2.0
    # X = np.random.uniform(low=-X_dist_bound, high=X_dist_bound, size=(nsim,nx))
    X = np.repeat(np.random.uniform(low=-X_dist_bound, high=X_dist_bound, size=(n_sim_p, nx)),
                       n_sim_w, axis=0)
    sequences = {
        "X_max": xmax_b*np.ones([nsim, nx]),
        "X_min": xmin_b*np.ones([nsim, nx]),
        "U_max": umax_b*np.ones([nsim, nu]),
        "U_min": umin_b*np.ones([nsim, nu]),
        "X": X,
        "Y": X,
        "w": w_sigma * np.random.randn(nsim, nx),  # additive uncertainties
        # "R": xr*np.ones([nsim, nx]),
        "R": np.ones([nsim, 1]),
        "U": np.random.randn(nsim, nu),
    }
    nstep_data, loop_data, dims = get_sequence_dataloaders(sequences, args.nsteps)
    train_data, dev_data, test_data = nstep_data
    train_loop, dev_loop, test_loop = loop_data

    """
    # # #  System model and Control policy
    """
    # Fully observable estimator as identity map: x0 = Yp[-1]
    # x_0 = Yp
    # Yp = [y_-N, ..., y_0]
    estimator = estimators.FullyObservable({**dims, "x0": (nx,)},
                                           nsteps=args.nsteps,  # future window Nf
                                           window_size=1,  # past window Np <= Nf
                                           input_keys=["Xp"],
                                           name='estimator')
    # full state feedback control policy
    # Uf = p(x_0)
    # Uf = [u_0, ..., u_N]
    activation = activations['relu']
    linmap = slim.maps['linear']
    block = blocks.MLP
    policy = policies.MLPPolicy(
        {f'x0_{estimator.name}': (nx,), **dims},
        nsteps=args.nsteps,
        bias=args.bias,
        linear_map=linmap,
        nonlin=activation,
        hsizes=[args.nx_hidden] * args.n_layers,
        input_keys=[f'x0_{estimator.name}'],
        name='policy',
    )

    # A, B, C linear maps
    fu = slim.maps['linear'](nu, nx)
    fx = slim.maps['linear'](nx, nx)
    fy = slim.maps['linear'](nx, nx)
    fd = slim.maps['linear'](nx, nx)

    # LTI SSM
    # x_k+1 = Ax_k + Bu_k
    # y_k+1 = Cx_k+1
    dynamics_model = dynamics.BlockSSM(fx, fy, fu=fu, fd=fd, name='dynamics',
                                       input_key_map={'x0': f'x0_{estimator.name}',
                                                   'Uf': 'U_pred_policy', 'Df': 'wf'})
    # model matrices values
    dynamics_model.fx.linear.weight = torch.nn.Parameter(torch.tensor(A, dtype=torch.float32))
    dynamics_model.fu.linear.weight = torch.nn.Parameter(torch.tensor(B, dtype=torch.float32))
    dynamics_model.fy.linear.weight = torch.nn.Parameter(torch.tensor(C, dtype=torch.float32))
    dynamics_model.fd.linear.weight = torch.nn.Parameter(torch.eye(nx, nx))
    # fix model parameters
    dynamics_model.requires_grad_(False)

    """
    # # #  DPC objectives and constraints
    """
    y = Variable(f"X_pred_{dynamics_model.name}")     # system outputs
    x = Variable(f"X_pred_{dynamics_model.name}")       # system states
    r = Variable("Rf")                                  # references
    u = Variable(f"U_pred_{policy.name}")
    # constraints bounds variables
    umin = Variable("U_minf")
    umax = Variable("U_maxf")
    xmin = Variable("X_minf")
    xmax = Variable("X_maxf")
    # weight factors of loss function terms and constraints
    Q_r = 20.0
    Q_s = 5.0
    Q_u = 0.0
    Q_dx = 0.0
    Q_du = 0.0
    Q_contract = 1.0

    Q_con_u = 2.0
    Q_con_x = 1.0
    # define loss function terms and constraints via neuromancer constraints syntax
    # reference_loss = Q_r*((r == y)^2)                   # track reference and stabilize
    action_loss = Q_u*((u==0)^2)                       # control penalty

    ctrl_idx = [2]
    reference_loss = Q_r*((r == y[:,:,ctrl_idx])^2)                   # track reference and stabilize
    stab_idx = [3,4,5,9,10,11]
    # stab_idx = [0,1,3,4,5,6,7,8,9,10,11]
    stabilize_loss = Q_s*((0 == y[:,:,stab_idx])^2)                   # track reference and stabilize

    control_smoothing = Q_du*((u[1:] == u[:-1])^2)          # delta u penalty
    state_smoothing = Q_dx*((x[1:] == x[:-1])^2)           # delta x penalty

    alpha = 0.8
    contraction = Loss(
        [f'X_pred_{dynamics_model.name}'],
        lambda x:
        torch.norm(F.relu(torch.norm(x[:,:,stab_idx], 2)
                          -alpha*torch.norm(x[:,:,stab_idx], 2)), 1),
        weight=Q_contract,
        name="contraction_loss")

    state_lower_bound_penalty = Q_con_x*(x > xmin)
    state_upper_bound_penalty = Q_con_x*(x < xmax)
    input_lower_bound_penalty = Q_con_u*(u > umin)
    input_upper_bound_penalty = Q_con_u*(u < umax)

    objectives = [reference_loss, stabilize_loss, contraction,
                  action_loss, control_smoothing, state_smoothing]
    constraints = [
        state_lower_bound_penalty,
        state_upper_bound_penalty,
        input_lower_bound_penalty,
        input_upper_bound_penalty,
    ]

    """
    # # #  DPC problem = objectives + constraints + trainable components 
    """
    # data (y_k) -> estimator (x_k) -> policy (u_k) -> dynamics (x_k+1, y_k+1)
    components = [estimator, policy, dynamics_model]
    # create constrained optimization loss
    loss = get_loss(objectives, constraints, args)
    # construct constrained optimization problem
    problem = Problem(components, loss)
    # plot computational graph
    problem.plot_graph()

    """
    # # #  DPC trainer 
    """
    # logger and metrics
    args.savedir = 'test_control'
    args.verbosity = 1
    metrics = ["nstep_dev_loss"]
    logger = BasicLogger(args=args, savedir=args.savedir, verbosity=args.verbosity, stdout=metrics)
    logger.args.system = 'dpc_ref'
    # device and optimizer
    device = f"cuda:{args.gpu}" if args.gpu is not None else "cpu"
    problem = problem.to(device)
    optimizer = torch.optim.AdamW(problem.parameters(), lr=args.lr)

    # trainer
    trainer = Trainer(
        problem,
        train_data,
        dev_data,
        test_data,
        optimizer,
        logger=logger,
        epochs=args.epochs,
        patience=args.patience,
        train_metric="nstep_train_loss",
        dev_metric="nstep_dev_loss",
        test_metric="nstep_test_loss",
        eval_metric='nstep_dev_loss',
        warmup=args.warmup,
    )
    # Train control policy
    best_model = trainer.train()
    best_outputs = trainer.test(best_model)

    """
    # # #  Plots and Analysis
    """
    # plot closed loop trajectories from different initial conditions
    cl_simulate(A, B, C, policy, nstep=50, w_sigma=0.02,
                x0=0*np.ones([nx, 1]), ref=sequences['R'],
                umin=umin_b, umax=umax_b, save_path='test_control')
    cl_simulate(A, B, C, policy, nstep=50, w_sigma=0.05,
                x0=0.2*np.ones([nx, 1]), ref=sequences['R'],
                umin=umin_b, umax=umax_b, save_path='test_control')

    print(f'Q_r {Q_r}')
    print(f'Q_u {Q_u}')
    print(f'Q_s {Q_s}')
    print(f'Q_du {Q_du}')
    print(f'Q_dx {Q_dx}')
    print(f'Q_con_u {Q_con_u}')
    print(f'Q_con_x {Q_con_x}')
    print(f'Q_contract {Q_contract}')
    print(f'alpha {alpha}')
    print(f'N {args.nsteps}')
    print(f'nx_hidden {args.nx_hidden}')
    print(f'n_layers {args.n_layers}')
    print(f'X_dist_bound {X_dist_bound}')
    print(f'lr {args.lr}')

    if args.validate:
        mu, mu_tilde, confidence, mu_lbound, I_s, I_c, X_c_mean, U_c_mean \
            = cl_validate(A, B, C, policy, n_sim_p=3333, n_sim_w=10, umin=umin_b, umax=umax_b)