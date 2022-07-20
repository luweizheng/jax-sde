import math
import time
from dataclasses import dataclass

import jax
import jax.lax as lax
import jax.numpy as jnp
import jax.random as jrandom
import optax 

from typing import Sequence, Callable

import equinox as eqx

class FNN(eqx.Module):
    mlp: eqx.nn.MLP

    def __init__(self, in_size, out_size, width_size, depth, *, key):
        self.mlp = eqx.nn.MLP(
            in_size, out_size, width_size=width_size, depth=depth, key=key
        )

    def __call__(self, t, x, train: bool = True):
        y, f_vjp = jax.vjp(lambda x: self.mlp(jnp.concatenate([t, x])), x)
        dudx = f_vjp(jnp.ones(y.shape))
        return y, dudx[0]


class FBSDEStep(eqx.Module):
    unet: FNN
    noise_size: int
    mu_fn: Callable = eqx.static_field()
    sigma_fn: Callable = eqx.static_field()
    phi_fn: Callable = eqx.static_field()

    def __init__(self, in_size, out_size, width_size, depth, noise_size, key):
        self.unet = FNN(in_size=in_size, out_size=out_size, width_size=width_size, depth=depth, key=key)
        def mu_fn(t, X, Y, Z):
            del t, Y, Z
            return jnp.zeros_like(X)

        def sigma_fn(t, X, Y):
            del t, Y
            return 0.4 * X

        def phi_fn(t, X, Y, Z):
            del t
            return 0.05 * (Y - jnp.sum(X * Z, keepdims=True))
        
        self.mu_fn = mu_fn
        self.sigma_fn = sigma_fn
        self.phi_fn = phi_fn
        self.noise_size = noise_size
        

    def u_and_dudx(self, t, x):
        return self.unet(t, x)

    def __call__(self, carry, inp):
        # `t` and `W` are (batch_size, num_timestep, dim)
        # it have input data across iterations 

        (i, t0, dt, x0, y0, z0, key) = carry

        key, key2 = jrandom.split(key)

        # use `i` to index input data
        curr_t = jnp.full((1, ), t0 + i * dt)
        next_t = jnp.full((1, ), t0 + (i + 1) * dt)

        dW = jrandom.normal(key, (self.noise_size, )) * jnp.sqrt(dt)

        x1 = x0 + self.mu_fn(curr_t, x0, y0, z0) * dt + \
                self.sigma_fn(curr_t, x0, y0) * dW
    
        
        y1_tilde = y0 + self.phi_fn(curr_t, x0, y0, z0) * dt + \
            jnp.sum(z0 * self.sigma_fn(curr_t, x0, y0) * dW, keepdims=True)
        
        y1, z1 = self.unet(next_t, x1)

        carry = (i+1, t0, dt, x1, y1, z1, key2)
        outputs = (x1, y1_tilde, y1)
        return carry, outputs


class NeuralFBSDE(eqx.Module):
    step: FBSDEStep
    hidden_size: int
    depth: int
    width_size: int
    
    def __init__(self, in_size, out_size, width_size, depth, noise_size, key):
        self.step = FBSDEStep(in_size, out_size, width_size, depth, noise_size, key)
        self.hidden_size  = in_size - 1
        self.depth = depth
        self.width_size = width_size


    def make_cost_model_feature(self):

        def step_fn(carry, inp):
            return self.step(carry, inp)

        output = ""

        dummy_t0 = 0.0
        dummy_dt = 0.2

        x0 = jnp.ones((self.hidden_size, ))
        y0, z0 = self.step.u_and_dudx(t=jnp.zeros((1, )), x=x0)
        
        dummy_bm_key = jrandom.PRNGKey(0)
        carry = (0, dummy_t0, dummy_dt, x0, y0, z0, dummy_bm_key)

        hlo_module = jax.xla_computation(step_fn)(carry, None).as_hlo_module()
        client = jax.lib.xla_bridge.get_backend()
        step_cost = jax.lib.xla_client._xla.hlo_module_cost_analysis(client, hlo_module)
        step_bytes_access_gb = step_cost['bytes accessed'] / 1e9
        step_flops_g = step_cost['flops'] / 1e9
        
        # step bytes access in GB
        output = output + str(step_bytes_access_gb) + ','
        # step G FLOPS 
        output = output + str(step_flops_g) + ','
        # step Arithmetic Intensity
        output = output + str(step_flops_g / step_bytes_access_gb) + ','

        total_params = sum(p.size for p in jax.tree_leaves(eqx.filter(self.step, eqx.is_array)))

        # total params
        output = output + str(total_params / 1e6) + ','

        # hidden_size: the dimension of DE
        output = output + str(self.hidden_size) + ','
        # noise_size: browian motion size ? 
        # TODO should we add this for ODE/CDE？
        # output = output + str(self.noise_size) + ','
        
        # width_size: width for every layer of MLP
        # output = output + str(self.width_size) + ','
        
        # depth: depth of MLP
        output = output + str(self.depth) + ','

        return output

    def __call__(self, x0, t0, dt, num_timesteps, unroll=1, key=jrandom.PRNGKey(0)):
        
        y0, z0 = self.step.u_and_dudx(t=jnp.zeros((1, )), x=x0)

        carry = (0, t0, dt, x0, y0, z0, key)

        def step_fn(carry, inp=None):
            return self.step(carry, inp)
        
        (carry, output) = jax.lax.scan(step_fn, carry, None, length=num_timesteps, unroll=unroll)
        return (carry, output)

@jax.jit
def sum_square_error(y, y_pred):
    """Computes the sum of square error."""
    return jnp.sum(jnp.square(y - y_pred))

# def fetch_minibatch(rng):  # Generate time + a Brownian motion
#     T = 1.0
#     M = batch_size
#     N = num_timesteps
#     D = dim

#     dt = T / N * jnp.ones((M, 1))
#     dW = jnp.sqrt(T / N) * jrandom.normal(rng, shape=(M, N, D))

#     return dt, dW

def g_fn(X):
    return jnp.sum(X ** 2, axis=-1, keepdims=True)

def dg_fn(X):
    y, vjp_func = jax.vjp(g_fn, X)
    return vjp_func(jnp.ones(y.shape))[0]


@eqx.filter_jit
def train_step(model, x0, t0, dt, num_timesteps, optimizer, opt_state, unroll=1, key=jrandom.PRNGKey(0)):
    # batch_size = model.batch_size
    # t, W = data
    @eqx.filter_jit
    def loss_fn(model):
        loss = 0.0
        
        out_carry, out_val = jax.vmap(model, in_axes=(0, None, None, None, None, None))(x0, t0, dt, num_timesteps, unroll, key)
        
        (_, _, _, x_final, y_final, z_final, _) = out_carry
        (x, y_tilde_list, y_list) = out_val
        
        loss += sum_square_error(y_tilde_list, y_list)
        loss += sum_square_error(y_final, g_fn(x_final))
        loss += sum_square_error(z_final, dg_fn(x_final))

        return (loss, y_list)

    (loss, y), grads = eqx.filter_value_and_grad(loss_fn, has_aux=True)(model)
    # (loss, y), grads = grad_loss(model, x0, t0, dt, num_timesteps, unroll, key)
    
    updates, opt_state = optimizer.update(grads, opt_state)
    model = eqx.apply_updates(model, updates)
    
    return loss, model, opt_state, y


def train(args):
    start_time = time.time()

    learning_rate = 1e-3
    rng = jrandom.PRNGKey(0)

    model = NeuralFBSDE(in_size=args.dim + 1, out_size=1, width_size=16, depth=4, noise_size=args.dim, key=rng)
    features = model.make_cost_model_feature()

    x0 = jnp.array([1.0, 0.5] * int(args.dim / 2))
    x0 = jnp.broadcast_to(x0, (args.batch_size, args.dim))

    optimizer = optax.adam(learning_rate)
    opt_state = optimizer.init(eqx.filter(model, eqx.is_array))

    verbose = True

    for i in range(args.num_iters):
        rng, _ = jax.random.split(rng)
        # data = fetch_minibatch(rng)
        # jax.vmap(model, in_axes=[0, None, None, None, None, None])(x0, 0.0, 0.01, 100, unroll, rng)
        loss, model, loss, y_pred = train_step(model, x0, 0.0, args.dt, args.num_timesteps, optimizer, opt_state, args.unroll, rng)

        if verbose:
            if i == 0:
                compile_time = time.time()
        
    features = features + str(args.batch_size) + ','
    features = features + str(args.unroll) + ','
    output = features + str(compile_time - start_time) + ','
    output = output + str(time.time() - compile_time)
    print(output)
    
    # print(f"compile: {compile_time - start_time}")
    # print(f"run: {time.time() - compile_time}")
    # output = output + str(compile_time - start_time) + ','
    # output = output + str(time.time() - compile_time)


@dataclass
class Args:
    batch_size: int
    dt: float

    # dim of SDE
    dim: int
    num_timesteps: int
    num_iters: int
    
    # network
    depth: Sequence[int]
    width_size: int
    
    # dynamic unroll
    unroll: int
    T: float = 1.0

def main(args):
    train(args)
    train(args)


if __name__ == '__main__':
    # test code
    args = Args(batch_size=128, 
                dt=0.2,
                dim=16,
                num_timesteps=50,
                num_iters=1000, 
                depth=3, 
                width_size=64,
                unroll=10)
    # warm up run
    main(args=args)
