from .TTrainClass import TTrain
import torch
import torch.nn as nn


class PosMPS(TTrain):
    """MPS model for tensor network with positive parameters.

    Uses absolute value of real parameters.
    """

    def __init__(
            self, dataset, d, D, 
            homogeneous=True, w_randomization=None, gradient_clipping_threshold=None,
            log_stability=True, 
            verbose=False):
        super().__init__(
            dataset, d, D, dtype=torch.float, 
            homogeneous=homogeneous, w_randomization=w_randomization,
            gradient_clipping_threshold=gradient_clipping_threshold,
            verbose=verbose)
        self.log_stability = log_stability
        self.name = "Positive MPS"
        self.short_name = "posMPS"
        if homogeneous:
            self.name += ", Homogeneous"
            self.short_name += " hom"
        else:
            self.name += ", Non-homogeneous"
            self.short_name += " non-hom"
        if not log_stability:
            self.name += " without log_stability"

    def _logprob(self, x):
        """Compute log probability of one configuration P(x)

        Args:
            x (np.ndarray): shape (seqlen,)

        Returns:
            logprob (torch.Tensor): size []
        """
        if self.log_stability:
            unnorm_logprob = self._log_contract_at(x)
            log_normalization = self._log_contract_all()
            logprob = unnorm_logprob - log_normalization
            # print(output, unnorm_prob, normalization, logprob)
        else:
            unnorm_prob = self._contract_at(x)
            normalization = self._contract_all()
            logprob = unnorm_prob.log() - normalization.log()
        return logprob

    def _logprob_batch(self, X):
        """Compute log P(x) for all x in a batch X

        Args:
            X : shape (batch_size, seqlen)

        Returns:
            logprobs (torch.Tensor): size [batchsize]
        """
        if self.log_stability:
            unnorm_logprobs = self._log_contract_at_batch(X) # tensor size [batchsize]
            # print(unnorm_logprobs)
            # print([self._log_contract_at(x).item() for x in X])
            log_normalization = self._log_contract_all() # scalar
            logprobs = unnorm_logprobs - log_normalization
        else:
            raise NotImplementedError('batched=True not implemented for log_stability=False')
        return logprobs

    def _contract_at(self, x):
        """Contract network at particular values in the physical dimension,
        for computing probability of x.
        """
        # repeat the core seqlen times
        if self.homogeneous:
            # repeat the core seqlen times
            w = self.core[None].repeat(self.seqlen, 1, 1, 1)
        else:
            w = self.core
        w2 = w.square()
        left_boundary2 = self.left_boundary.square()
        right_boundary2 = self.right_boundary.square()
        # contract the network, from the left boundary through to the last core
        contracting_tensor = left_boundary2
        for i in range(self.seqlen):
            contracting_tensor = torch.einsum(
                'i, ij -> j',
                contracting_tensor,
                w2[i, x[i], :, :])
            if contracting_tensor.min() < 0:
                print("contraction < 0")
                print(w.min())
        # contract the final bond dimension
        output = torch.einsum(
            'i, i ->', contracting_tensor, right_boundary2)
        # if self.verbose:
        #     print("contract_at", output)
        if output < 0:
            print("output of contract_at < 0")
        return output

    def _contract_all(self):
        """Contract network with a copy of itself across physical index,
        for computing norm.
        """
        # repeat the core seqlen times
        if self.homogeneous:
            # repeat the core seqlen times
            w = self.core[None].repeat(self.seqlen, 1, 1, 1)
        else:
            w = self.core
        w2 = w.square()
        left_boundary2 = self.left_boundary.square()
        right_boundary2 = self.right_boundary.square()
        # first, left boundary contraction
        # (note: if real-valued conj will have no effect)
        contracting_tensor = torch.einsum(
            'j, ijk -> k', left_boundary2, w2[0, :, :, :])
        # contract the network
        for i in range(1, self.seqlen):
            contracting_tensor = torch.einsum(
                'j, ijk -> k',
                contracting_tensor,
                w2[i, :, :, :])
        # contract the final bond dimension with right boundary vector
        output = torch.dot(contracting_tensor, right_boundary2)
        # if self.verbose:
        #     print("contract_all", output)
        return output

    def _log_contract_at(self, x):
        """Contract network at particular values in the physical dimension,
        for computing probability of x.
        """
        if self.homogeneous:
            # repeat the core seqlen times
            w = self.core[None].repeat(self.seqlen, 1, 1, 1)
        else:
            w = self.core
        w2 = w.square()
        left_boundary2 = self.left_boundary.square()
        right_boundary2 = self.right_boundary.square()
        Z = self.vec_norm(left_boundary2)
        contractor_unit = left_boundary2 / Z
        accumulated_lognorm = Z.log()
        # contract the network, from the left boundary through to the last core
        for i in range(self.seqlen):
            contractor_temp = torch.einsum(
                'i, ij -> j',
                contractor_unit,
                w2[i, x[i], :, :])
            Z = self.vec_norm(contractor_temp)
            contractor_unit = contractor_temp / Z
            accumulated_lognorm += Z.log()
            if contractor_unit.min() < 0:
                print("contraction < 0")
                print(w.min())
        # contract the final bond dimension
        output = torch.einsum(
            'i, i ->', contractor_unit, right_boundary2)
        logprob = accumulated_lognorm + output.log()
        # if self.verbose:
        #     print("contract_at", output)
        if output < 0:
            print("output of contract_at < 0")
        return logprob

    def _log_contract_at_batch(self, X):
        """Contract network at particular values in the physical dimension,
        for computing probability of x, for x in X.
        input:
            X: tensor batch of observations, size [batch_size, seq_len]
        returns:
            logprobs: tensor of log probs, size [batch_size]
        Uses log norm stability trick.
        """
        batch_size = X.shape[0]
        if self.homogeneous:
            # repeat the core seqlen times, and repeat that batch_size times
            w = self.core[(None,)*2].repeat(batch_size, self.seqlen, 1, 1, 1)
        else:
            # repeat nonhomogenous core batch_size times
            w = self.core[None].repeat(batch_size, 1, 1, 1, 1)
        w2 = w.square() # w shape is [batch_size, seqlen, d, D, D]
        left_boundaries2 = self.left_boundary[None].repeat(batch_size, 1).square()
        right_boundaries2 = self.right_boundary[None].repeat(batch_size, 1).square()
        # normalizers, one per batch 
        Zs, _ = left_boundaries2.max(axis=1) # do vec_norm on each row (!note infinity norm is hardcoded here)
        contractor_unit = left_boundaries2 / Zs[:,None]
        accumulated_lognorms = Zs.log()
        # make one hot encoding of data, and select along physical dimension of weights
        Xh = torch.nn.functional.one_hot(X, num_classes=self.d)
        w2_selected = (w2 * Xh[:, :, :, None, None]).sum(2) # w2_selected shape is [batchsize, seqlen, D, D]
        # contract the network, from the left boundary through to the last core
        for i in range(self.seqlen):
            contractor_temp = torch.einsum(
                'bi, bij -> bj',
                contractor_unit,
                w2_selected[:, i, :, :])
            Zs, _ = contractor_temp.abs().max(axis=1)
            contractor_unit = contractor_temp / Zs[:,None]
            accumulated_lognorms += Zs.log()
        # contract the final bond dimension
        output = torch.einsum(
            'bi, bi -> b', contractor_unit, right_boundaries2)
        logprobs = accumulated_lognorms + output.log()  # shape [batchsize]
        if (output < 0).any():
            print("Warning! output of contract_at contains negative values...")
        return logprobs

    def _log_contract_all(self):
        """Contract network with a copy of itself across physical index,
        for computing norm.
        """
        # repeat the core seqlen times
        if self.homogeneous:
            # repeat the core seqlen times
            w = self.core[None].repeat(self.seqlen, 1, 1, 1)
        else:
            w = self.core
        w2 = w.square()
        left_boundary2 = self.left_boundary.square()
        right_boundary2 = self.right_boundary.square()
        Z = self.vec_norm(left_boundary2)
        contractor_unit = left_boundary2 / Z
        accumulated_lognorm = Z.log()
        # first, left boundary contraction
        # (note: if real-valued conj will have no effect)
        contractor_temp = torch.einsum(
            'j, ijk -> k', contractor_unit, w2[0, :, :, :])
        Z = self.vec_norm(contractor_temp)
        contractor_unit = contractor_temp / Z
        accumulated_lognorm += Z.log()
        # contract the network
        for i in range(1, self.seqlen):
            contractor_temp = torch.einsum(
                'j, ijk -> k',
                contractor_unit,
                w2[i, :, :, :])
            Z = self.vec_norm(contractor_temp)
            contractor_unit = contractor_temp / Z
            accumulated_lognorm += Z.log()
        # contract the final bond dimension with right boundary vector
        output = torch.dot(contractor_unit, right_boundary2)
        logprob = accumulated_lognorm + output.log()
        # if self.verbose:
        #     print("contract_all", output)
        return logprob


class Born(TTrain):
    """Born model for tensor network with real or complex parameters.

    Parameters:
        dtype ([tensor.dtype]): 
            tensor.float for real, or tensor.cfloat for complex
    """
    def __init__(
            self, dataset, d, D, dtype, 
            homogeneous=True, w_randomization=None, gradient_clipping_threshold=None,
            log_stability=True, verbose=False):
        super().__init__(
            dataset, d, D, dtype, 
            homogeneous=homogeneous, w_randomization=w_randomization, 
            gradient_clipping_threshold=gradient_clipping_threshold,
            verbose=verbose)
        self.log_stability = log_stability
        self.name = f"Born ({dtype})"
        if dtype==torch.cfloat:
            prefix = 'c' 
        elif dtype==torch.float:
            prefix = 'r'
        self.short_name = prefix+"Born"
        if homogeneous:
            self.name += ", Homogeneous"
            self.short_name += " hom"
        else:
            self.name += ", Non-homogeneous"
            self.short_name += " non-hom"
        if not log_stability:
            self.name += " without log_stability"

    def _logprob(self, x):
        """Compute log probability of one configuration P(x)

        Args:
            x (np.ndarray): shape (seqlen,)

        Returns:
            logprob (torch.Tensor): size []
        """
        if self.log_stability:
            unnorm_logprob = self._log_contract_at(x)
            log_normalization = self._log_contract_all()
            logprob = unnorm_logprob - log_normalization
            # print(output, unnorm_prob, normalization, logprob)
        else:
            output = self._contract_at(x)
            unnorm_prob = output.abs().square()
            normalization = self._contract_all().abs()
            logprob = unnorm_prob.log() - normalization.log()
        return logprob

    def _logprob_batch(self, X):
        """Compute log P(x) for all x in a batch X

        Args:
            X : shape (batch_size, seqlen)

        Returns:
            logprobs (torch.Tensor): size [batchsize]
        """
        if self.log_stability:
            unnorm_logprobs = self._log_contract_at_batch(X) # tensor size [batchsize]
            # print(unnorm_logprobs)
            # print([self._log_contract_at(x).item() for x in X])
            log_normalization = self._log_contract_all() # scalar
            logprobs = unnorm_logprobs - log_normalization
        else:
            raise NotImplementedError('batched=True not implemented for log_stability=False')
        return logprobs