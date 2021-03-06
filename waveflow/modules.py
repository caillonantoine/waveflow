import torch
import torch.nn as nn
import numpy as np
from tqdm import tqdm

from . import hparams as hp
from .fast_utils import CircularTensor

def full_flip(x):
    """
    Perform a tensor flip on the height dimension (BxCxHxW)
    """
    return torch.flip(x, (2,))

def half_flip(x):
    """
    Split a tensor alongside the height dimension, and flip them
    """
    x1,x2 = torch.split(x, x.shape[2]//2, 2)
    x1 = torch.flip(x1,(2,))
    x2 = torch.flip(x2,(2,))
    return torch.cat([x1,x2], 2)

class ResidualBlock(nn.Module):
    """
    Wavenet inspired 2D residual block, with a causal constraint built-in the initial convolution layer.

    Parameters
    ----------

    dilation: int
        Dilation factor used in the initial conv
    """
    def __init__(self, dilation):
        super().__init__()
        self.dilation = dilation

        total_size = (hp.kernel_size - 1) * dilation + 1
        self.padding_h  = total_size - 1 
        self.padding_w  = total_size // 2


        self.initial_conv = nn.Conv2d(hp.res_size, hp.hidden_size * 2,
                                      hp.kernel_size, dilation=dilation,
                                      padding=(self.padding_h, self.padding_w), bias=False)

        self.cdtconv      = nn.Conv2d(hp.cdt_size, hp.hidden_size * 2, 1, bias=False)
        
        self.resconv      = nn.Conv2d(hp.hidden_size, hp.res_size, 1, bias=False)
        self.skipconv     = nn.Conv2d(hp.hidden_size, hp.skp_size, 1, bias=False)


        self.apply_weight_norm()


    def forward(self, x, c, incremental=False):
        """
        Forward pass

        Parameters
        ----------

        x: Tensor
            Input signal to process (BxCxHxW)
        c: Tensor
            Conditioning signal (BxCxHxW)

        Returns
        -------

        res: Tensor
            Residual connexion

        skip: Tensor
            Skip connexion
        """
        res = x.clone()

        if incremental:
            if self.initial_conv.padding[0] != 0:
                self.initial_conv.padding = 0,self.initial_conv.padding[1]

            x = self.initial_conv(x)
        else:
            x = self.initial_conv(x)[:,:,:-self.padding_h,:]

        c = self.cdtconv(c)

        xa,xb = torch.split(x, hp.hidden_size, 1)
        ca,cb = torch.split(c, hp.hidden_size, 1)

        x = torch.tanh(xa + ca) * torch.sigmoid(xb + cb)


        if incremental:
            res = self.resconv(x) + res[:,:,-1:,:]
        else:
            res = self.resconv(x) + res

        skp = self.skipconv(x)

        return res, skp
    
    def apply_weight_norm(self):
        self.initial_conv = nn.utils.weight_norm(self.initial_conv)
        self.cdtconv     = nn.utils.weight_norm(self.cdtconv)
        self.resconv      = nn.utils.weight_norm(self.resconv)
        self.skipconv     = nn.utils.weight_norm(self.skipconv)

    def remove_weight_norm(self):
        nn.utils.remove_weight_norm(self.initial_conv)
        nn.utils.remove_weight_norm(self.cdtconv)
        nn.utils.remove_weight_norm(self.resconv)
        nn.utils.remove_weight_norm(self.skipconv)


class ResidualStack(nn.Module):
    """
    Stack of residual blocks, alongside pre and post conv, inspired by Wavenet.
    """
    def __init__(self):
        super().__init__()

        self.first_conv = nn.Conv2d(hp.in_size, hp.res_size, (2,1),
                                    padding=(2,0), bias=False)

        self.stack = nn.ModuleList([
            ResidualBlock(2**i) for i in np.arange(hp.n_layer) % hp.cycle_size
        ])

        self.last_convs = nn.Sequential(
            nn.ReLU(),
            nn.Conv2d(hp.skp_size, hp.skp_size, 1, bias=False),
            nn.ReLU(),
            nn.Conv2d(hp.skp_size, hp.out_size, 1, bias=False)
        )

        self.cache = None


        self.apply_weight_norm()
    
    def forward(self, x, c):
        """
        Forward pass

        Parameters
        ----------

        x: Tensor
            Input signal to process (BxCxHxW)
        c: Tensor
            Conditioning signal (BxCxHxW)

        Returns
        -------

        Tensor
            Output of the residual stack
        """

        res = torch.tanh(self.first_conv(x)[:,:,:-3,:])
        skp_list = []

        for resblock in self.stack:
            res, skp = resblock(res, c)
            skp_list.append(skp)
        
        x = sum(skp_list)

        return self.last_convs(x)
    
    def arTransform(self, z, c):
        """
        Auto-regressive forward pass, caching intermediate states to optimize both memory usage and
        generation speed

        Parameters
        ----------

        z: Tensor
            Input signal to transform

        c: Tensor
            Conditioning signal
        
        Returns
        -------

        Tensor
            Transformed signal
        """
        device = next(self.parameters()).device

        if self.cache is None:
            # FIRST SYNTHESIS
            self.first_conv.padding = (0,0)
            self.cache = []
            for block in self.stack:
                self.cache.append(torch.zeros(z.shape[0],
                                              hp.res_size,
                                              (hp.kernel_size-1)*block.dilation + 1,
                                              z.shape[-1]).type(z.dtype).to(device))
            for i in range(len(self.cache)):
                self.cache[i] = CircularTensor(self.cache[i], 2)
        else:
            # RESUMED SYNTHESIS, RESET CACHE
            for i in range(len(self.cache)):
                self.cache[i].tensor.zero_()

        z = nn.functional.pad(z, (0,0,2,0))

        for step in range(hp.h):
            # GETTING INPUTS ################################
            z_in = z[:,:,step:step+2,:]
            c_in = c[:,:,step:step+1,:]
            #################################################

            # COMPUTING PARAMETRIZATION #####################
            res = torch.tanh(self.first_conv(z_in))
            skp_list = []

            for i,resblock in enumerate(self.stack):
                self.cache[i].set_current(res[:,:,-1,:])
                res = self.cache[i]()
                res, skp = resblock(res, c_in, incremental=True)
                skp_list.append(skp)

            x = sum(skp_list)

            mean, logvar = torch.split(self.last_convs(x),1,1)
            #################################################

            # UPDATING OUTPUT ###############################
            z[:,:,step+2,:] = (z[:,:,step+2,:] - mean[:,:,-1,:]) * torch.exp(-logvar[:,:,-1,:])
            #################################################

        return z[...,2:,:]

    def apply_weight_norm(self):
        for i in [1,3]:
            self.last_convs[i] = nn.utils.weight_norm(self.last_convs[i])

    def remove_weight_norm(self):
        for i in [1,3]:
            nn.utils.remove_weight_norm(self.last_convs[i])
        for s in self.stack:
            s.remove_weight_norm()


class WaveFlow(nn.Module):
    """
    Implementation of the WaveFlow model. Each parameter is defined inside waveflow/hparams.py
    """
    def __init__(self, verbose=False):
        super().__init__()
        self.flows = nn.ModuleList([
            ResidualStack() for i in range(hp.n_flow)
        ])

        self.receptive_field = (hp.kernel_size-1)*(sum([2**(i%hp.cycle_size) for i in range(hp.n_layer)])) + 1

        skipped = 0
        for p in self.parameters():
            try:
                nn.init.xavier_normal_(p)
            except:
                skipped += 1

        if verbose:
            print(f"Skipped {skipped} parameters during initialisation")
            print(f"Built waveflow with squeezed height {hp.h} and receptive field {self.receptive_field}")

    def forward(self, x, c, squeezed=False):
        """
        Forward pass of the waveflow model ( z = f(x) )

        Parameters
        ----------

        x: Tensor
            Signal to be transformed

        c: Tensor
            Conditioning signal

        squeeze: bool
            Boolean to define weither x.shape = BxN or x.shape = Bx1xHxW

        Returns
        -------

        x: Tensor
            Transformed signal

        global_mean: Tensor
            Transformed mean

        global_logvar: Tensor
            Transformed logvar

        """
        if not squeezed:
            x = x.reshape(x.shape[0], 1, x.shape[-1] // hp.h, -1).transpose(2,3)
            c = c.reshape(c.shape[0], c.shape[1], c.shape[-1] // hp.h, -1).transpose(2,3)

        global_mean    = None
        global_logvar  = None

        for i,flow in enumerate(self.flows):          
            mean, logvar = torch.split(flow(x,c), 1, 1)

            logvar = torch.clamp(logvar, max=10)
            
            if global_mean is not None and global_logvar is not None:
                global_mean    = global_mean * torch.exp(logvar) + mean
                global_logvar  = global_logvar + logvar
            
            else:
                global_mean   = mean
                global_logvar = logvar

            x = torch.exp(logvar) * x + mean

            x = full_flip(x) if i < 4 else half_flip(x)
            c = full_flip(c) if i < 4 else half_flip(c)

        return x, global_mean, global_logvar

    def loss(self, x, c):
        """
        Negative log likelihood loss implementation
        """

        z, mean, logvar = self.forward(x,c)
      
        loss = torch.mean(z ** 2 - logvar)
        
        return z, mean, logvar, loss

    def synthesize(self, c, temp=1.0):
        """
        Reverse pass of the waveflow model ( x = f-1(x) )
        Synthesis of a signal given a condition

        Parameters
        ----------

        c: Tensor
            Conditioning signal
        
        temp: float
            Variance (or temperature) of the input noise

        Returns
        -------

        z: Tensor
            Synthesized signal

        """
        c = c.reshape(c.shape[0], c.shape[1], c.shape[-1] // hp.h, -1).transpose(2,3)
        z = torch.randn(c.shape[0], 1, c.shape[2], c.shape[3]).type(c.dtype).to(c.device)
    
        z = (z * temp)

        for i,flow in enumerate(tqdm(self.flows[::-1], desc="Iterating overs flows")):
            z = full_flip(z) if i > 4 else half_flip(z)
            c = full_flip(c) if i > 4 else half_flip(c)
            
            for step in range(hp.h):
                z_in = z[:,:,:step+1,:]
                c_in = c[:,:,:step+1,:]

                mean, logvar = torch.split(flow(z_in,c_in), 1, 1)

                z[:,:,step,:] = (z[:,:,step,:] - mean[:,:,-1,:]) * torch.exp(-logvar[:,:,-1,:])
      
            
        z = z.transpose(2,3).reshape(z.shape[0], -1)

        return z


    def synthesize_fast(self, c, temp=1.0):
        """
        Reverse pass of the waveflow model ( x = f-1(x) )
        Synthesis of a signal given a condition

        This method uses cached convolution to optimize the memory usage and
        the generation speed.

        ONLY USE DURING EVALUATION AS IT MODIFIES INTERNAL CONVOLUTIONS AND
        MAY BREAK TRAINING

        Parameters
        ----------

        c: Tensor
            Conditioning signal
        
        temp: float
            Variance (or temperature) of the input noise

        Returns
        -------

        z: Tensor
            Synthesized signal

        """
        c = c.reshape(c.shape[0], c.shape[1], c.shape[-1] // hp.h, -1).transpose(2,3)
        z = torch.randn(c.shape[0], 1, c.shape[2], c.shape[3]).type(c.dtype).to(c.device)
        z = (z * temp)


        for i,flow in enumerate(tqdm(self.flows[::-1], desc="Iterating overs flows")):
            z = full_flip(z) if i > 4 else half_flip(z)
            c = full_flip(c) if i > 4 else half_flip(c)

            z = flow.arTransform(z,c)
            
        z = z.transpose(2,3).reshape(z.shape[0], -1)

        return z

    def remove_weight_norm(self):
        for f in self.flows:
            f.remove_weight_norm()
