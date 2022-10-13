from typing import Any, Callable, Dict, List, Optional, Union

import gym
import numpy as np
from tqdm.auto import trange
from typing_extensions import Protocol

from ..dataset import TransitionMiniBatch
from ..logger import LOG, D3RLPyLogger
from ..metrics.scorer import evaluate_on_environment
from ..preprocessing import ActionScaler, Scaler
from ..preprocessing.stack import StackedObservation
from .buffers import Buffer,ReplayBuffer
from .explorers import Explorer


class AlgoProtocol(Protocol):
	def update(self, batch: TransitionMiniBatch) -> Dict[str, float]:
		...

	def build_with_env(self, env: gym.Env) -> None:
		...

	def save_params(self, logger: D3RLPyLogger) -> None:
		...

	def predict(self, x: Union[np.ndarray, List[Any]]) -> np.ndarray:
		...

	def sample_action(self, x: Union[np.ndarray, List[Any]]) -> np.ndarray:
		...

	def save_model(self, fname: str) -> None:
		...

	def set_active_logger(self, logger: D3RLPyLogger) -> None:
		...

	@property
	def action_size(self) -> Optional[int]:
		...

	@property
	def scaler(self) -> Optional[Scaler]:
		...

	@property
	def action_scaler(self) -> Optional[ActionScaler]:
		...

	@property
	def n_frames(self) -> int:
		...

	@property
	def n_steps(self) -> int:
		...

	@property
	def gamma(self) -> float:
		...

	@property
	def batch_size(self) -> int:
		...

	@property
	def impl(self) -> Optional[Any]:
		...

	@property
	def grad_step(self) -> int:
		...

def scale_action(action_space, action):
	"""
	Rescale the action from [low, high] to [-1, 1]
	(no need for symmetric action space)
	:param action_space: (gym.spaces.box.Box)
	:param action: (np.ndarray)
	:return: (np.ndarray)
	"""
	action = np.clip(action, action_space.low, action_space.high)
	low, high = action_space.low, action_space.high
	return 2.0 * ((action - low) / (high - low + 1e-8)) - 1.0


def unscale_action(action_space, scaled_action):
	"""
	Rescale the action from [-1, 1] to [low, high]
	(no need for symmetric action space)
	:param action_space: (gym.spaces.box.Box)
	:param action: (np.ndarray)
	:return: (np.ndarray)
	"""
	low, high = action_space.low, action_space.high
	return low + (0.5 * (scaled_action + 1.0) * (high - low + 1e-8))
def _setup_algo(algo: AlgoProtocol, env: gym.Env) -> None:
	# initialize scaler
	if algo.scaler:
		LOG.debug("Fitting scaler...", scler=algo.scaler.get_type())
		algo.scaler.fit_with_env(env)

	# initialize action scaler
	if algo.action_scaler:
		LOG.debug(
			"Fitting action scaler...",
			action_scler=algo.action_scaler.get_type(),
		)
		algo.action_scaler.fit_with_env(env)

	# setup algorithm
	if algo.impl is None:
		LOG.debug("Building model...")
		algo.build_with_env(env)
		LOG.debug("Model has been built.")
	else:
		LOG.warning("Skip building models since they're already built.")


def train_single_env(
	algo: AlgoProtocol,
	env: gym.Env,
	buffer: Buffer,
	explorer: Optional[Explorer] = None,
	n_steps: int = 1000000,
	n_steps_per_epoch: int = 10000,
	update_interval: int = 1,
	update_start_step: int = 0,
	random_steps: int = 0,
	eval_env: Optional[gym.Env] = None,
	eval_epsilon: float = 0.0,
	save_metrics: bool = True,
	save_interval: int = 1,
	experiment_name: Optional[str] = None,
	with_timestamp: bool = True,
	logdir: str = "d3rlpy_logs",
	verbose: bool = True,
	show_progress: bool = True,
	tensorboard_dir: Optional[str] = None,
	timelimit_aware: bool = True,
	callback: Optional[Callable[[AlgoProtocol, int, int], None]] = None,
	load_demo=None,
	bc_loss=False,
    utd=1,
    dropout=0.0,
    layernorm=False,
) -> None:
	"""Start training loop of online deep reinforcement learning.

	Args:
		algo: algorithm object.
		env: gym-like environment.
		buffer : replay buffer.
		explorer: action explorer.
		n_steps: the number of total steps to train.
		n_steps_per_epoch: the number of steps per epoch.
		update_interval: the number of steps per update.
		update_start_step: the steps before starting updates.
		random_steps: the steps for the initial random explortion.
		eval_env: gym-like environment. If None, evaluation is skipped.
		eval_epsilon: :math:`\\epsilon`-greedy factor during evaluation.
		save_metrics: flag to record metrics. If False, the log
			directory is not created and the model parameters are not saved.
		save_interval: the number of epochs before saving models.
		experiment_name: experiment name for logging. If not passed,
			the directory name will be ``{class name}_online_{timestamp}``.
		with_timestamp: flag to add timestamp string to the last of
			directory name.
		logdir: root directory name to save logs.
		verbose: flag to show logged information on stdout.
		show_progress: flag to show progress bar for iterations.
		tensorboard_dir: directory to save logged information in
				tensorboard (additional to the csv data).  if ``None``, the
				directory will not be created.
		timelimit_aware: flag to turn ``terminal`` flag ``False`` when
			``TimeLimit.truncated`` flag is ``True``, which is designed to
			incorporate with ``gym.wrappers.TimeLimit``.
		callback: callable function that takes ``(algo, epoch, total_step)``
				, which is called at the end of epochs.

	"""
	# setup logger
	if experiment_name is None:
		experiment_name = algo.__class__.__name__ + "_online"

	logger = D3RLPyLogger(
		experiment_name,
		save_metrics=save_metrics,
		root_dir=logdir,
		verbose=verbose,
		tensorboard_dir=tensorboard_dir,
		with_timestamp=with_timestamp,
	)
	algo.set_active_logger(logger)

	# initialize algorithm parameters
	_setup_algo(algo, env)

	observation_shape = env.observation_space.shape
	is_image = len(observation_shape) == 3

	# prepare stacked observation
	if is_image:
		stacked_frame = StackedObservation(observation_shape, algo.n_frames)

	# save hyperparameters
	algo.save_params(logger)

	# switch based on show_progress flag
	xrange = trange if show_progress else range

	# setup evaluation scorer
	eval_scorer: Optional[Callable[..., float]]
	if eval_env:
		eval_scorer = evaluate_on_environment(eval_env, epsilon=eval_epsilon)
	else:
		eval_scorer = None
	# load demo
	if load_demo is not None:

		demos = np.load(load_demo,allow_pickle=True)
		if bc_loss:
			print("demo buffer created")
			demo_buffer = ReplayBuffer(1000000, env=env)

			for demo in demos:
				demo_steps = len(demo['obs'])
				for i in range(demo_steps):
					demo_buffer.append(
						observation=demo['obs'][i],
						action=scale_action(env.action_space, demo['act'][i]),
						reward=demo['rew'][i],
						terminal=demo['done'][i],
						clip_episode=demo['done'][i],
					)
		else:
			for demo in demos:
				demo_steps = len(demo['obs'])
				for i in range(demo_steps):
					buffer.append(
						observation=demo['obs'][i],
						action=scale_action(env.action_space, demo['act'][i]),
						reward=demo['rew'][i],
						terminal=demo['done'][i],
						clip_episode=demo['done'][i],
					)


		# import pickle
		# raw_dataset = pickle.load(open(load_demo, 'rb'))
		# import pdb;pdb.set_trace()
		# demo_num = len(raw_dataset)

		# for i in range(demo_num):
		# 	horizon = min(100,raw_dataset[i]['observations'].shape[0])
		# 	for j in range(horizon):

		# 		if j == horizon - 1:
		# 			terminals = True
		# 			episode_terminals = True
		# 		else:
		# 			terminals = False
		# 			episode_terminals = False

		# 		buffer.append(
		# 			observation=np.array(raw_dataset[i]['observations'][j][:11],dtype=np.float32),
		# 			action=np.array(scale_action(env.action_space, raw_dataset[i]['actions'][j]),dtype=np.float32),
		# 			reward=np.array(raw_dataset[i]['rewards'][j],dtype=np.float32),
		# 			terminal=np.array(terminals,dtype=np.float32),
		# 			clip_episode=np.array(episode_terminals,dtype=np.float32),
		# 		)



		print("demo loaded")
	# start training loop
	observation = env.reset()
	rollout_return = 0.0
	for total_step in xrange(1, n_steps + 1):
		with logger.measure_time("step"):
			# stack observation if necessary
			if is_image:
				stacked_frame.append(observation)
				fed_observation = stacked_frame.eval()
			else:
				observation = observation.astype("f4")
				fed_observation = observation

			# sample exploration action
			with logger.measure_time("inference"):

				if total_step < random_steps:
					# import pdb;pdb.set_trace()
					# actions sampled from action space are from range specific to the environment
					# but algorithm operates on tanh-squashed actions therefore simple scaling is used
					unscaled_action = env.action_space.sample()
					action = scale_action(env.action_space, unscaled_action)

				elif explorer:
					x = fed_observation.reshape((1,) + fed_observation.shape)
					action = explorer.sample(algo, x, total_step)[0]
				else:
					# import pdb;pdb.set_trace()
					action = algo.sample_action([fed_observation])[0]
					# # Add noise to the action (improve exploration,
					# # not needed in general)
					# if self.action_noise is not None:
					#     action = np.clip(action + self.action_noise(), -1, 1)
					# inferred actions need to be transformed to environment action_space before stepping
					unscaled_action = unscale_action(env.action_space, action)

			# step environment
			with logger.measure_time("environment_step"):
				next_observation, reward, terminal, info = env.step(unscaled_action)
				rollout_return += reward
			# special case for TimeLimit wrapper
			if timelimit_aware and "TimeLimit.truncated" in info:
				clip_episode = True
				terminal = False
			else:
				clip_episode = terminal
			# store observation
			buffer.append(
				observation=observation,
				action=action,
				reward=reward,
				terminal=terminal,
				clip_episode=clip_episode,
			)

			# reset if terminated
			if clip_episode:
				observation = env.reset()
				logger.add_metric("rollout_return", rollout_return)
				rollout_return = 0.0
				# for image observation
				if is_image:
					stacked_frame.clear()
			else:
				observation = next_observation

			# psuedo epoch count
			epoch = total_step // n_steps_per_epoch
			if total_step > update_start_step and len(buffer) > algo.batch_size:
				if total_step % update_interval == 0:
					# sample mini-batch
					with logger.measure_time("sample_batch"):
						demo_batch = None
						if bc_loss:
							buffer_t = buffer.sample_trainsitions(
								batch_size=algo.batch_size,
								n_frames=algo.n_frames,
								n_steps=algo.n_steps,
								gamma=algo.gamma,
							)
							demo_t = demo_buffer.sample_trainsitions(
								batch_size=128,
								n_frames=algo.n_frames,
								n_steps=algo.n_steps,
								gamma=algo.gamma,
							)
							batch = buffer.to_minibatch(
								buffer_t+demo_t,
								n_frames=algo.n_frames,
								n_steps=algo.n_steps,
								gamma=algo.gamma,
							)
							demo_batch = demo_buffer.to_minibatch(
								demo_t,
								n_frames=algo.n_frames,
								n_steps=algo.n_steps,
								gamma=algo.gamma,
							)
						else:
							batch = buffer.sample(
								batch_size=algo.batch_size,
								n_frames=algo.n_frames,
								n_steps=algo.n_steps,
								gamma=algo.gamma,
							)
					# update parameters
					with logger.measure_time("algorithm_update"):
						for _ in range(1):
							loss = algo.update(batch,demo_batch=demo_batch,utd=utd)

					# record metrics
					for name, val in loss.items():
						logger.add_metric(name, val)

			# call callback if given
			if callback:
				callback(algo, epoch, total_step)

		if epoch > 0 and total_step % n_steps_per_epoch == 0:
			# evaluation
			if eval_scorer:
				logger.add_metric("evaluation", eval_scorer(algo))

			if epoch % save_interval == 0:
				logger.save_model(total_step, algo)

			# save metrics
			logger.commit(epoch, total_step)

	# clip the last episode
	buffer.clip_episode()

	# close logger
	logger.close()


def collect(
	algo: AlgoProtocol,
	env: gym.Env,
	buffer: Buffer,
	explorer: Optional[Explorer] = None,
	deterministic: bool = False,
	n_steps: int = 1000000,
	show_progress: bool = True,
	timelimit_aware: bool = True,
) -> None:
	"""Collects data via interaction with environment.

	Args:
		algo: algorithm object.
		env: gym-like environment.
		buffer : replay buffer.
		explorer: action explorer.
		deterministic: flag to collect data with the greedy policy.
		n_steps: the number of total steps to train.
		show_progress: flag to show progress bar for iterations.
		timelimit_aware: flag to turn ``terminal`` flag ``False`` when
			``TimeLimit.truncated`` flag is ``True``, which is designed to
			incorporate with ``gym.wrappers.TimeLimit``.

	"""
	# initialize algorithm parameters
	_setup_algo(algo, env)

	observation_shape = env.observation_space.shape
	is_image = len(observation_shape) == 3

	# prepare stacked observation
	if is_image:
		stacked_frame = StackedObservation(observation_shape, algo.n_frames)

	# switch based on show_progress flag
	xrange = trange if show_progress else range

	# start training loop
	observation = env.reset()
	for total_step in xrange(1, n_steps + 1):
		# stack observation if necessary
		if is_image:
			stacked_frame.append(observation)
			fed_observation = stacked_frame.eval()
		else:
			observation = observation.astype("f4")
			fed_observation = observation

		# sample exploration action
		if deterministic:
			action = algo.predict([fed_observation])[0]
		else:
			if explorer:
				x = fed_observation.reshape((1,) + fed_observation.shape)
				action = explorer.sample(algo, x, total_step)[0]
			else:
				action = algo.sample_action([fed_observation])[0]

		# step environment
		next_observation, reward, terminal, info = env.step(action)

		# special case for TimeLimit wrapper
		if timelimit_aware and "TimeLimit.truncated" in info:
			clip_episode = True
			terminal = False
		else:
			clip_episode = terminal

		# store observation
		buffer.append(
			observation=observation,
			action=action,
			reward=reward,
			terminal=terminal,
			clip_episode=clip_episode,
		)

		# reset if terminated
		if clip_episode:
			observation = env.reset()
			# for image observation
			if is_image:
				stacked_frame.clear()
		else:
			observation = next_observation

	# clip the last episode
	buffer.clip_episode()
