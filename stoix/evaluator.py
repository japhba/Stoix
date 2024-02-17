from typing import Any, Dict, Optional, Tuple

import chex
import flax.linen as nn
import jax
import jax.numpy as jnp
from flax.core.frozen_dict import FrozenDict
from jumanji.env import Environment
from omegaconf import DictConfig

from stoix.types import (
    ActorApply,
    EvalFn,
    EvalState,
    ExperimentOutput,
    RecActorApply,
    RNNEvalState,
)
from stoix.utils.jax import unreplicate_batch_dim


def get_ff_evaluator_fn(
    env: Environment,
    apply_fn: ActorApply,
    config: DictConfig,
    log_win_rate: bool = False,
    eval_multiplier: int = 1,
) -> EvalFn:
    """Get the evaluator function for feedforward networks.

    Args:
        env (Environment): An evironment isntance for evaluation.
        apply_fn (callable): Network forward pass method.
        config (dict): Experiment configuration.
        eval_multiplier (int): A scalar that will increase the number of evaluation
            episodes by a fixed factor. The reason for the increase is to enable the
            computation of the `absolute metric` which is a metric computed and the end
            of training by rolling out the policy which obtained the greatest evaluation
            performance during training for 10 times more episodes than were used at a
            single evaluation step.
    """

    def eval_one_episode(params: FrozenDict, init_eval_state: EvalState) -> Dict:
        """Evaluate one episode. It is vectorized over the number of evaluation episodes."""

        def _env_step(eval_state: EvalState) -> EvalState:
            """Step the environment."""
            # PRNG keys.
            key, env_state, last_timestep, step_count, episode_return = eval_state

            # Select action.
            key, policy_key = jax.random.split(key)

            pi = apply_fn(
                params, jax.tree_map(lambda x: x[jnp.newaxis, ...], last_timestep.observation)
            )

            if config.arch.evaluation_greedy:
                action = pi.mode()
            else:
                action = pi.sample(seed=policy_key)

            # Step environment.
            env_state, timestep = env.step(env_state, action.squeeze())

            # Log episode metrics.
            episode_return += timestep.reward
            step_count += 1
            eval_state = EvalState(key, env_state, timestep, step_count, episode_return)
            return eval_state

        def not_done(carry: Tuple) -> bool:
            """Check if the episode is done."""
            timestep = carry[2]
            is_not_done: bool = ~timestep.last()
            return is_not_done

        final_state = jax.lax.while_loop(not_done, _env_step, init_eval_state)

        eval_metrics = {
            "episode_return": final_state.episode_return,
            "episode_length": final_state.step_count,
        }
        # Log won episode if win rate is required.
        if log_win_rate:
            eval_metrics["won_episode"] = jnp.all(final_state.timestep.reward >= 1.0).astype(int)

        return eval_metrics

    def evaluator_fn(trained_params: FrozenDict, key: chex.PRNGKey) -> ExperimentOutput[EvalState]:
        """Evaluator function."""

        # Initialise environment states and timesteps.
        n_devices = len(jax.devices())

        eval_batch = (config.arch.num_eval_episodes // n_devices) * eval_multiplier

        key, *env_keys = jax.random.split(key, eval_batch + 1)
        env_states, timesteps = jax.vmap(env.reset)(
            jnp.stack(env_keys),
        )
        # Split keys for each core.
        key, *step_keys = jax.random.split(key, eval_batch + 1)
        # Add dimension to pmap over.
        step_keys = jnp.stack(step_keys).reshape(eval_batch, -1)

        eval_state = EvalState(
            key=step_keys,
            env_state=env_states,
            timestep=timesteps,
            step_count=jnp.zeros((eval_batch, 1)),
            episode_return=jnp.zeros_like(timesteps.reward),
        )

        eval_metrics = jax.vmap(
            eval_one_episode,
            in_axes=(None, 0),
            axis_name="eval_batch",
        )(trained_params, eval_state)

        return ExperimentOutput(
            learner_state=eval_state,
            episode_metrics=eval_metrics,
            train_metrics={},
        )

    return evaluator_fn


def get_rnn_evaluator_fn(
    env: Environment,
    apply_fn: RecActorApply,
    config: DictConfig,
    scanned_rnn: nn.Module,
    log_win_rate: bool = False,
    eval_multiplier: int = 1,
) -> EvalFn:
    """Get the evaluator function for recurrent networks."""

    def eval_one_episode(params: FrozenDict, init_eval_state: RNNEvalState) -> Dict:
        """Evaluate one episode. It is vectorized over the number of evaluation episodes."""

        def _env_step(eval_state: RNNEvalState) -> RNNEvalState:
            """Step the environment."""
            (
                key,
                env_state,
                last_timestep,
                last_done,
                hstate,
                step_count,
                episode_return,
            ) = eval_state

            # PRNG keys.
            key, policy_key = jax.random.split(key)

            # Add a batch dimension and env dimension to the observation.
            batched_observation = jax.tree_util.tree_map(
                lambda x: jnp.expand_dims(x, axis=0)[jnp.newaxis, :], last_timestep.observation
            )
            ac_in = (batched_observation, jnp.expand_dims(last_done, axis=0))

            # Run the network.
            hstate, pi = apply_fn(params, hstate, ac_in)

            if config.arch.evaluation_greedy:
                action = pi.mode()
            else:
                action = pi.sample(seed=policy_key)

            # Step environment.
            env_state, timestep = env.step(env_state, action[-1].squeeze(0))

            # Log episode metrics.
            episode_return += timestep.reward
            step_count += 1
            eval_state = RNNEvalState(
                key,
                env_state,
                timestep,
                timestep.last().reshape(-1),
                hstate,
                step_count,
                episode_return,
            )
            return eval_state

        def not_done(carry: Tuple) -> bool:
            """Check if the episode is done."""
            timestep = carry[2]
            is_not_done: bool = ~timestep.last()
            return is_not_done

        final_state = jax.lax.while_loop(not_done, _env_step, init_eval_state)

        eval_metrics = {
            "episode_return": final_state.episode_return,
            "episode_length": final_state.step_count,
        }
        # Log won episode if win rate is required.
        if log_win_rate:
            eval_metrics["won_episode"] = jnp.all(final_state.timestep.reward >= 1.0).astype(int)
        return eval_metrics

    def evaluator_fn(
        trained_params: FrozenDict, key: chex.PRNGKey
    ) -> ExperimentOutput[RNNEvalState]:
        """Evaluator function."""

        # Initialise environment states and timesteps.
        n_devices = len(jax.devices())

        eval_batch = config.arch.num_eval_episodes // n_devices * eval_multiplier

        key, *env_keys = jax.random.split(key, eval_batch + 1)
        env_states, timesteps = jax.vmap(env.reset)(jnp.stack(env_keys))
        # Split keys for each core.
        key, *step_keys = jax.random.split(key, eval_batch + 1)
        # Add dimension to pmap over.
        step_keys = jnp.stack(step_keys).reshape(eval_batch, -1)

        # Initialise hidden state.
        init_hstate = scanned_rnn.initialize_carry(
            eval_batch, config.network.actor_network.pre_torso.layer_sizes[-1]
        )
        init_hstate = jnp.expand_dims(init_hstate, axis=1)

        # Initialise dones.
        dones = jnp.zeros(
            (eval_batch, 1),
            dtype=bool,
        )

        eval_state = RNNEvalState(
            key=step_keys,
            env_state=env_states,
            timestep=timesteps,
            dones=dones,
            hstate=init_hstate,
            step_count=jnp.zeros((eval_batch, 1)),
            episode_return=jnp.zeros_like(timesteps.reward),
        )

        eval_metrics = jax.vmap(
            eval_one_episode,
            in_axes=(None, 0),
            axis_name="eval_batch",
        )(trained_params, eval_state)

        return ExperimentOutput(
            learner_state=eval_state,
            episode_metrics=eval_metrics,
            train_metrics={},
        )

    return evaluator_fn


def evaluator_setup(
    eval_env: Environment,
    key_e: chex.PRNGKey,
    network: Any,
    params: FrozenDict,
    config: DictConfig,
    use_recurrent_net: bool = False,
    scanned_rnn: Optional[nn.Module] = None,
) -> Tuple[EvalFn, EvalFn, Tuple[FrozenDict, chex.Array]]:
    """Initialise evaluator_fn."""
    # Get available TPU cores.
    n_devices = len(jax.devices())
    # Check if win rate is required for evaluation.
    log_win_rate = False
    # Vmap it over number of agents and create evaluator_fn.
    if use_recurrent_net:
        assert scanned_rnn is not None
        eval_apply_fn = network.apply
        evaluator = get_rnn_evaluator_fn(
            eval_env,
            eval_apply_fn,
            config,
            scanned_rnn,
            log_win_rate,
        )
        absolute_metric_evaluator = get_rnn_evaluator_fn(
            eval_env,
            eval_apply_fn,
            config,
            scanned_rnn,
            log_win_rate,
            10,
        )
    else:
        eval_apply_fn = network.apply
        evaluator = get_ff_evaluator_fn(eval_env, eval_apply_fn, config, log_win_rate)
        absolute_metric_evaluator = get_ff_evaluator_fn(
            eval_env,
            eval_apply_fn,
            config,
            log_win_rate,
            10,
        )

    evaluator = jax.pmap(evaluator, axis_name="device")
    absolute_metric_evaluator = jax.pmap(absolute_metric_evaluator, axis_name="device")

    # Broadcast trained params to cores and split keys for each core.
    trained_params = unreplicate_batch_dim(params)
    key_e, *eval_keys = jax.random.split(key_e, n_devices + 1)
    eval_keys = jnp.stack(eval_keys).reshape(n_devices, -1)

    return evaluator, absolute_metric_evaluator, (trained_params, eval_keys)