import torch
from torch.utils.data import Dataset
from torch.utils.data.dataloader import default_collate
from scipy.io import loadmat
import numpy as np
import pandas as pd

from neuromancer.data.normalization import norm_fns


def _extract_var(data, regex):
    filtered = data.filter(regex=regex).values
    return filtered if filtered.values.size != 0 else None


def read_file(file_path):
    file_type = file_path.split(".")[-1].lower()
    if file_type == "mat":
        f = loadmat(file_path)
        Y = f.get("y", None)  # outputs
        U = f.get("u", None)  # inputs
        D = f.get("d", None)  # disturbances
        id_ = f.get("exp_id", None)  # experiment run id
    elif file_type == "csv":
        data = pd.read_csv(file_path)
        Y = _extract_var(data, "y[0-9]*")
        U = _extract_var(data, "u[0-9]*")
        D = _extract_var(data, "d[0-9]*")
        id_ = _extract_var(data, "exp_id")
    else:
        print(f"error: unsupported file type: {file_type}")

    return {
        k: v for k, v in zip(["Y", "U", "D", "exp_id"], [Y, U, D, id_]) if v is not None
    }


def batch_tensor(x: torch.Tensor, steps: int, mh: bool = False):
    return x.unfold(0, steps, 1 if mh else steps)


def unbatch_tensor(x: torch.Tensor, mh: bool = False):
    return (
        torch.cat((x[:, :, :, 0], x[-1, :, :, 1:].permute(2, 0, 1)), dim=0)
        if mh
        else torch.cat(torch.unbind(x, 0), dim=-1).permute(2, 0, 1)
    )


class SequenceDataset(Dataset):
    def __init__(
        self,
        data,
        nsteps=1,
        moving_horizon=False,
        name="data",
    ):
        """
        :param data: (dict str: np.array) dictionary mapping variable names to tensors of shape (T, Dk),
            where T is number of time steps and Dk is dimensionality of variable k.
        :param nsteps: (int) N-step prediction horizon for batching data.
        :param moving_horizon: (bool) if True, generate batches using sliding window with stride 1; else
            use stride N.
        """
        super().__init__()

        self.name = name

        self.full_data = torch.cat(
            [torch.tensor(v, dtype=torch.float) for v in data.values()], dim=1
        )
        self.dims = {k: v.shape for k, v in data.items()}
        self.variables = list(data.keys())

        i = 0
        self._vslices = {}
        for k, v in self.dims.items():
            self._vslices[k] = slice(i, i + v[1], 1)
            i += v[1]

        self.nsteps = nsteps
        self.nsim = self.full_data.shape[0]

        self.dims = {
            **self.dims,
            **{k + "p": (self.nsim, v[1]) for k, v in self.dims.items()},
            **{k + "f": (self.nsim, v[1]) for k, v in self.dims.items()},
            "nsim": self.nsim,
            "nsteps": nsteps,
        }

        self.batched_data = batch_tensor(self.full_data, nsteps, mh=moving_horizon)
        self.batched_data = self.batched_data.permute(0, 2, 1).contiguous()

    def __len__(self):
        return len(self.batched_data) - 1

    def __getitem__(self, i):
        return {
            **{
                k + "p": self.batched_data[i, :, self._vslices[k]]
                for k in self.variables
            },
            **{
                k + "f": self.batched_data[i + 1, :, self._vslices[k]]
                for k in self.variables
            },
        }

    def get_full_sequence(self):
        return {
            **{
                k + "p": self.full_data[:-self.nsteps, self._vslices[k]].unsqueeze(1)
                for k in self.variables
            },
            **{
                k + "f": self.full_data[self.nsteps:, self._vslices[k]].unsqueeze(1)
                for k in self.variables
            },
            "name": "loop_" + self.name,
        }

    def collate_fn(self, batch):
        batch = default_collate(batch)
        return {**{k: v.transpose(0, 1) for k, v in batch.items()}, "name": "nstep_" + self.name}


def normalize_data(data, norm_type, stats=None):
    if stats is None:
        norm_fn = lambda x, _: norm_fns[norm_type](x)
    else:
        norm_fn = lambda x, k: norm_fns[norm_type](
            x,
            stats[k + "_min"].reshape(1, -1),
            stats[k + "_max"].reshape(1, -1),
        )

    norm_data = [norm_fn(v, k) for k, v in data.items()]
    norm_data, stat0, stat1 = zip(*norm_data)
    return {k: v for k, v in zip(data.keys(), norm_data)}, {
        **{k + "_min": v for k, v in zip(data.keys(), stat0)},
        **{k + "_max": v for k, v in zip(data.keys(), stat1)},
    }


def split_data(data, split_ratio=None):
    nsim = min(v.shape[0] for v in data.values())
    if split_ratio is None:
        split_len = nsim // 3
        train_offs = slice(0, split_len)
        dev_offs = slice(split_len, split_len * 2)
        test_offs = slice(split_len * 2, nsim)
    else:
        train_offs = slice(0, int(split_ratio[0] / 100) * nsim)
        dev_offs = slice(
            train_offs[1], train_offs[1] + int(split_ratio[1] / 100) * nsim
        )
        test_offs = slice(dev_offs[1], nsim)

    train_data = {k: v[train_offs] for k, v in data.items()}
    dev_data = {k: v[dev_offs] for k, v in data.items()}
    test_data = {k: v[test_offs] for k, v in data.items()}

    return train_data, dev_data, test_data


def get_sequence_dataloaders(data, nsteps, norm_type="zero-one", split_ratio=None):
    """This will generate dataloaders and open-loop sequence dictionaries for a
    given dictionary of data. Dataloaders are hard-coded for full-batch training
    to match NeuroMANCER's original training setup.

    :param data: (dict str: np.array)
    :param nsteps: (int)
    :param norm_type: (str)
    :param split_ratio: (list int)
    """

    data, _ = normalize_data(data, norm_type)
    train_data, dev_data, test_data = split_data(data, split_ratio)

    # train_data, train_stats = normalize_data(train_data, "zero-one")
    # dev_data, _ = normalize_data(dev_data, "zero-one", train_stats)
    # test_data, _ = normalize_data(test_data, "zero-one", train_stats)

    train_data = SequenceDataset(train_data, nsteps=nsteps, name="train")
    dev_data = SequenceDataset(dev_data, nsteps=nsteps, name="dev")
    test_data = SequenceDataset(test_data, nsteps=nsteps, name="test")

    train_loop = train_data.get_full_sequence()
    dev_loop = dev_data.get_full_sequence()
    test_loop = test_data.get_full_sequence()

    train_data = torch.utils.data.DataLoader(
        train_data,
        batch_size=len(train_data),
        collate_fn=train_data.collate_fn,
    )
    dev_data = torch.utils.data.DataLoader(
        dev_data,
        batch_size=len(dev_data),
        collate_fn=dev_data.collate_fn,
    )
    test_data = torch.utils.data.DataLoader(
        test_data,
        batch_size=len(test_data),
        collate_fn=test_data.collate_fn,
    )

    return (train_data, dev_data, test_data), (train_loop, dev_loop, test_loop)


if __name__ == "__main__":
    import psl
    import slim
    import torch.nn.functional as F

    from neuromancer import (
        blocks,
        estimators,
        dynamics,
        simulators,
        problem,
        trainer,
        loggers,
        callbacks,
    )

    # emu = psl.emulators["TwoTank"](nsim=10000, ninit=0, seed=408)
    # data = emu.simulate()

    data = read_file(psl.datasets["aero"])
    nstep_data, loop_data = get_sequence_dataloaders(data, 32)
    train_data, dev_data, test_data = nstep_data
    train_loop, dev_loop, test_loop = loop_data

    nx = train_data.dataset.dims["Y"][-1]

    print("dims", train_data.dataset.dims)
    print("nx", nx)

    activation = torch.nn.GELU
    linmap = slim.maps["linear"]
    linargs = {"sigma_min": 0.1, "sigma_max": 1.0}

    nonlinmap = blocks.MLP

    estimator = estimators.MLPEstimator(
        {**train_data.dataset.dims, "x0": (nx * 32,)},
        nsteps=32,
        window_size=8,
        bias=False,
        linear_map=linmap,
        nonlin=activation,
        hsizes=[nx * 32] * 2,
        input_keys=["Yp"],
        linargs=linargs,
        name="estim",
    )
    dynamics_model = dynamics.block_model(
        "hammerstein",
        {**train_data.dataset.dims, "x0_estim": (nx * 32,)},
        linmap,
        nonlinmap,
        bias=False,
        n_layers=2,
        activation=activation,
        name="dynamics",
        input_keys={"x0": f"x0_{estimator.name}"},
        linargs=linargs,
    )

    xmin = -0.2
    xmax = 1.2
    dxudmin = -0.05
    dxudmax = 0.05
    estimator_loss = problem.Objective(
        [f"X_pred_{dynamics_model.name}", f"x0_{estimator.name}"],
        lambda X_pred, x0: F.mse_loss(X_pred[-1, :-1, :], x0[1:]),
        weight=1.0,
        name="arrival_cost",
    )
    regularization = problem.Objective(
        [f"reg_error_{estimator.name}", f"reg_error_{dynamics_model.name}"],
        lambda reg1, reg2: reg1 + reg2,
        weight=0,
        name="reg_error",
    )
    reference_loss = problem.Objective(
        [f"Y_pred_{dynamics_model.name}", "Yf"], F.mse_loss, weight=1.0, name="ref_loss"
    )
    state_smoothing = problem.Objective(
        [f"X_pred_{dynamics_model.name}"],
        lambda x: F.mse_loss(x[1:], x[:-1]),
        weight=0.1,
        name="state_smoothing",
    )
    observation_lower_bound_penalty = problem.Objective(
        [f"Y_pred_{dynamics_model.name}"],
        lambda x: torch.mean(F.relu(-x + xmin)),
        weight=0.1,
        name="y_low_bound_error",
    )
    observation_upper_bound_penalty = problem.Objective(
        [f"Y_pred_{dynamics_model.name}"],
        lambda x: torch.mean(F.relu(x - xmax)),
        weight=0.1,
        name="y_up_bound_error",
    )
    inputs_max_influence_lb = problem.Objective(
        [f"fU_{dynamics_model.name}"],
        lambda x: torch.mean(F.relu(-x + dxudmin)),
        weight=0.1,
        name="input_influence_lb",
    )
    inputs_max_influence_ub = problem.Objective(
        [f"fU_{dynamics_model.name}"],
        lambda x: torch.mean(F.relu(x - dxudmax)),
        weight=0.1,
        name="input_influence_ub",
    )
    objectives = [regularization, reference_loss] #, estimator_loss]
    constraints = [
        state_smoothing,
        observation_lower_bound_penalty,
        observation_upper_bound_penalty,
        inputs_max_influence_lb,
        inputs_max_influence_ub,
    ]

    model = problem.Problem(objectives, constraints, [estimator, dynamics_model])
    model = model.to("cpu")
    logger = loggers.BasicLogger(
        args=None,
        savedir="test",
        verbosity=1,
        stdout=(
            "nstep_train_loss",
            "best_nstep_train_ref_loss",
            "nstep_train_ref_loss",
            "nstep_dev_ref_loss",
            "best_nstep_dev_ref_loss",
            "loop_dev_ref_loss",
            "best_loop_dev_ref_loss",
        ),
    )

    simulator = simulators.OpenLoopSimulator(
        model,
        train_loop,
        dev_loop,
        test_loop,
        eval_sim=True
    )
    callback = callbacks.SysIDCallback(simulator, None)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.002)
    trainer = trainer.Trainer(
        model,
        train_data,
        dev_data,
        test_data,
        optimizer,
        epochs=100,
        patience=100,
        logger=logger,
        callback=callback,
        eval_metric="nstep_dev_ref_loss",
    )
    best_model = trainer.train()
    best_outputs = trainer.test(best_model)
