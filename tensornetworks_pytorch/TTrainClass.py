import torch
import torch.nn as nn
import torch.optim as optim
from tqdm import tqdm
from torch.utils.data import Dataset, DataLoader

class TTrain(nn.Module):
    """Abstract class for Tensor Train models.  Use instantiating class.

    Parameters:
        D (int): bond dimension
        d (int): physical dimension (number of categories in data)
        dtype ([tensor.dtype]): 
            tensor.float for real, or tensor.cfloat for complex
    """
    def __init__(self, dataset, d, D, dtype, homogeneous=True, w_randomization=None, verbose=False):
        super().__init__()
        self.D = D
        self.d = d
        self.verbose = verbose
        self.homogeneous = homogeneous
        self.dataset = dataset
        self.n_datapoints = dataset.shape[0]
        self.seqlen = dataset.shape[1]

        # choose weight initialization scheme
        if w_randomization == 'noisy':
            w_init = self.noisy_ones  # constant at 1, with some noise
        elif w_randomization== 'random_angle':
            w_init = self.randomsign_ones  # 1 * +/-(/+j/-j)
        else:
            w_init = torch.ones  # constant at 1

        # the following are set to nn.Parameters thus are backpropped over
        k_core = (d*D*D)**-0.5 
        k_vectors = (d)**-0.5
        if homogeneous: # initialize single core to be repeated
            core = k_core * w_init((d, D, D), dtype=dtype)
            #core = torch.randn(d, D, D, dtype=dtype)
            self.core = nn.Parameter(core)
        else: # initialize seqlen different non-homogeneous cores
            core = k_core * w_init((self.seqlen, d, D, D), dtype=dtype)
            #core = torch.randn(self.seqlen, d, D, D, dtype=dtype)
            self.core = nn.Parameter(core)
        left_boundary = k_vectors * w_init(D, dtype=dtype)
        #left_boundary = torch.randn(D, dtype=dtype)
        self.left_boundary = nn.Parameter(left_boundary)
        right_boundary = k_vectors * w_init(D, dtype=dtype)
        #right_boundary = torch.randn(D, dtype=dtype)
        self.right_boundary = nn.Parameter(right_boundary)

    @staticmethod
    def noisy_ones(shape, dtype=torch.float):
        """Fill from gaussian with mean 1, variance hardcoded."""
        x = torch.ones(shape, dtype=dtype)
        e = 0.5 * torch.randn(shape, dtype=dtype)
        return x + e

    @staticmethod
    def randomsign_ones(shape, dtype=torch.float):
        """Makes a vector of ones with random sign, 
        or if dtype is torch.cfloat, randomized real or imaginary units"""
        x = torch.zeros(shape)
        if dtype==torch.cfloat:
            random4=torch.randint_like(x,4)
            r = x + 1*(random4==0) - 1*(random4==1) 
            i = x + 1*(random4==2) - 1*(random4==3)
            out = torch.complex(r,i)
        else:
            random2=torch.randint_like(x,2)
            out = x + 1*(random2==0) - 1*(random2==1) 
        return torch.tensor(out, dtype=dtype)

    def mat_norm(self, mat):
        """Our norm for matrices: infinity norm"""
        # equivalent to torch.linalg.norm(vec, ord=float('inf')).real
        return torch.max(torch.sum(abs(mat), dim=1))
        
    def vec_norm(self, vec):
        """Our norm for vectors: infinity norm"""
        # equivalent to torch.linalg.norm(vec, ord=float('inf')).real
        return vec.abs().max()

    def _contract_at(self, x):
        """Contract network at particular values in the physical dimension,
        for computing probability of x.
        """
        if self.homogeneous:
            # repeat the core seqlen times
            w = self.core[None].repeat(self.seqlen, 1, 1, 1)
        else:
            w = self.core
        # contract the network, from the left boundary through to the last core
        contracting_tensor = self.left_boundary
        for i in range(self.seqlen):
            contracting_tensor = torch.einsum(
                'i, ij -> j',
                contracting_tensor,
                w[i, x[i], :, :])
        # contract the final bond dimension
        output = torch.einsum(
            'i, i ->', contracting_tensor, self.right_boundary)
        # if self.verbose:
        #     print("contract_at", output)
        return output

    def _contract_all(self):
        """Contract network with a copy of itself across physical index,
        for computing norm.
        """

        if self.homogeneous:
            # repeat the core seqlen times
            w = self.core[None].repeat(self.seqlen, 1, 1, 1)
        else:
            w = self.core

        # first, left boundary contraction
        # (note: if real-valued conj will have no effect)
        contracting_tensor = torch.einsum(
            'ij, ik -> jk',
            torch.einsum(
                'j, ijk -> ik', self.left_boundary, w[0, :, :, :]),
            torch.einsum(
                'j, ijk -> ik', self.left_boundary, w[0, :, :, :].conj())
        )
        # contract the network
        for i in range(1, self.seqlen):
            contracting_tensor = torch.einsum(
                'ij, ijkl -> kl',
                contracting_tensor,
                torch.einsum(
                    'ijk, ilm -> jlkm',
                    w[i, :, :, :],
                    w[i, :, :, :].conj()))
        # contract the final bond dimension with right boundary vector
        output = torch.einsum(
            'ij, i, j ->',
            contracting_tensor,
            self.right_boundary,
            self.right_boundary.conj())
        # if self.verbose:
        #     print("contract_all", output)
        return output

    def _log_contract_at(self, x):
        """Contract network at particular values in the physical dimension,
        for computing probability of x.
        Uses log norm stability trick.
        RETURNS A LOG PROB.
        """
        if self.homogeneous:
            # repeat the core seqlen times
            w = self.core[None].repeat(self.seqlen, 1, 1, 1)
        else:
            w = self.core
        # contract the network, from the left boundary through to the last core
        Z = self.vec_norm(self.left_boundary)
        contractor_unit = self.left_boundary / Z
        accumulated_lognorm = Z.log()
        for i in range(self.seqlen):
            contractor_temp = torch.einsum(
                'i, ij -> j',
                contractor_unit,
                w[i, x[i], :, :])
            Z = self.vec_norm(contractor_temp)
            contractor_unit = contractor_temp / Z
            accumulated_lognorm += Z.log()
        # contract the final bond dimension
        output = torch.einsum(
            'i, i ->', contractor_unit, self.right_boundary)
        output = (accumulated_lognorm.exp()*output).abs().square()
        logprob = output.log()
        # if self.verbose:
        #     print("contract_at", output)
        return logprob

    def _log_contract_all(self):
        """Contract network with a copy of itself across physical index,
        for computing norm.
        """

        if self.homogeneous:
            # repeat the core seqlen times
            w = self.core[None].repeat(self.seqlen, 1, 1, 1)
        else:
            w = self.core

        # first, left boundary contraction
        # (note: if real-valued conj will have no effect)
        Z = self.vec_norm(self.left_boundary)
        contractor_unit = self.left_boundary / Z
        accumulated_lognorm = Z.log()
        contractor_temp = torch.einsum(
            'ij, ik -> jk',
            torch.einsum(
                'j, ijk -> ik', contractor_unit, w[0, :, :, :]),
            torch.einsum(
                'j, ijk -> ik', contractor_unit, w[0, :, :, :].conj())
        )
        Z = self.mat_norm(contractor_temp)
        contractor_unit = contractor_temp / Z
        accumulated_lognorm += Z.log()
        # contract the network
        for i in range(1, self.seqlen):
            contractor_temp = torch.einsum(
                'ij, ijkl -> kl',
                contractor_unit,
                torch.einsum(
                    'ijk, ilm -> jlkm',
                    w[i, :, :, :],
                    w[i, :, :, :].conj()))
            Z = self.mat_norm(contractor_temp)
            contractor_unit = contractor_temp / Z
            accumulated_lognorm += Z.log()
        # contract the final bond dimension with right boundary vector
        output = torch.einsum(
            'ij, i, j ->',
            contractor_unit,
            self.right_boundary,
            self.right_boundary.conj())
        lognorm = (accumulated_lognorm.exp()*output).abs().log()
        # if self.verbose:
        #     print("contract_all", output)
        return lognorm

    def _log_contract_at_batch(self, X):
        """Contract network at particular values in the physical dimension,
        for computing probability of x, for x in X.
        input:
            X: tensor batch of observations, size [batch_size, seq_len]
        returns:
            logprobs: tensor of log probs, size [batch_size]
        Uses log norm stability trick.
        RETURNS LOG PROBS.
        """
        batch_size = X.shape[0]
        if self.homogeneous:
            # repeat the core seqlen times, and repeat that batch_size times
            w = self.core[(None,)*2].repeat(batch_size, self.seqlen, 1, 1, 1)
        else:
            # repeat nonhomogenous core batch_size times
            w = self.core[None].repeat(batch_size, 1, 1, 1)
        # contract the network, from the left boundary through to the last core
        left_boundaries = self.left_boundary[None].repeat(batch_size, 1)
        right_boundaries = self.right_boundary[None].repeat(batch_size, 1)
        Zs = torch.ones(batch_size, dtype=torch.float) # normalizers one per batch
        for b in range(batch_size):
            Zs[b] = self.vec_norm(left_boundaries[b])
        contractor_unit = left_boundaries / Zs
        accumulated_lognorms = Zs.log()
        for i in range(self.seqlen):
            contractor_temp = torch.einsum(
                'bi, bij -> bj',
                contractor_unit,
                w[:, i, x[i], :, :])
            for b in range(batch_size):
                Zs[b] = self.vec_norm(contractor_temp[b])
            contractor_unit = contractor_temp / Zs
            accumulated_lognorms += Zs.log()
        # contract the final bond dimension
        output = torch.einsum(
            'bi, bi -> b', contractor_unit, right_boundaries)
        probs = (accumulated_lognorms.exp() * output).abs().square()
        logprobs = probs.log()
        return logprobs

    def _logprob(self, x):
        """Compute log probability of one configuration P(x)

        Args:
            x : shape (seqlen,)

        Returns:
            logprob (torch.Tensor): size [1]
        """
        pass

    def _logprob_batch(self, X):
        """Compute log P(x) for all x in a batch X

        Args:
            X : shape (batch_size, seqlen)

        Returns:
            logprob (torch.Tensor): size [1]
        """
        pass

    def forward(self, x):
        return self._logprob(x)

    def forward_batch(self, batch):
        # TODO: av_logprob = self._logprob_batch(batch)
        return av_logprob

    def train(
            self, batchsize, max_epochs, 
            plot=False, tqdm=tqdm, device='cpu',
            optimizer=torch.optim.Adadelta, **optim_kwargs):
        dataset = self.dataset
        model = self.to(device)
        trainloader = DataLoader(dataset, batch_size=batchsize, shuffle=True)
        optimizer = optimizer(model.parameters(), **optim_kwargs)
        early_stopping_threshold = 1e-6 # min difference in epoch loss 
        loss_values = [] # store by-epoch avg loss values
        print(f'╭───────────────────────────\n│Training {self.name}, on {device}')
        print(f'│         batchsize:{batchsize}, {optimizer.__module__}, {optim_kwargs}.')
        av_batch_loss_running = -1e4
        with tqdm(range(max_epochs), unit="epoch", leave=True) as tepochs:
            for epoch in tepochs:
                batch_loss_list = []
                with tqdm(trainloader, unit="batch", leave=False, desc=f"epoch {epoch}") as tepoch:
                    for batch in tepoch:
                        for pindex, p in enumerate(model.parameters()):
                            if torch.isnan(p).any():
                                pnames = list(self.state_dict().keys())
                                print("│ loss values:", *(f"{x:.3f}" for x in loss_values))
                                print(f"└────Stopped before epoch {epoch}. NaN in weights {pnames[pindex]}!")
                                if plot:
                                    plt.plot(loss_values)
                                    plt.show()
                                return loss_values
                        model.zero_grad()
                        neglogprob = 0
                        for x in batch:
                            out = model(x.to(device))
                            neglogprob -= out
                        loss = neglogprob / len(batch)
                        loss.backward()
                        # for pindex, p in enumerate(model.parameters()):
                        #     if torch.isnan(p.grad).any():
                        #         pnames = list(self.state_dict().keys())
                        #         print("│ loss values:", *(f"{x:.3f}" for x in loss_values))
                        #         print(f"└────Stopped. NaN value in gradient for {pnames[pindex]}!")
                        #         if plot:
                        #             plt.plot(loss_values)
                        #             plt.show()
                        #         return loss_values
                        optimizer.step()
                        tepoch.set_postfix(loss=loss.item())
                        batch_loss_list.append(loss.item())
                    av_batch_loss = torch.Tensor(batch_loss_list).mean().item()
                    loss_values.append(av_batch_loss)
                    tepochs.set_postfix(av_batch_loss=av_batch_loss)
                    if abs(av_batch_loss_running - av_batch_loss) < early_stopping_threshold:
                        print(f"├────Early stopping after epoch {epoch}/{max_epochs}.")
                        break
                    av_batch_loss_running = av_batch_loss
        print("│ loss values:", *(f"{x:.3f}" for x in loss_values))
        if plot:
            plt.plot(loss_values)
            plt.show()
        print('│ Finished training.\n╰───────────────────────────\n')
        return loss_values
