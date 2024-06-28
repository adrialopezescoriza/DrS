ALGO_NAME = 'DrS-learn-reward'

import argparse
import os
import random
import time
from distutils.util import strtobool

os.environ["OMP_NUM_THREADS"] = "1"

import gymnasium as gym
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from stable_baselines3.common.buffers import ReplayBuffer
from torch.utils.tensorboard import SummaryWriter

import datetime
from collections import defaultdict

def parse_args():
    # fmt: off
    parser = argparse.ArgumentParser()
    parser.add_argument("--exp-name", type=str, default='test',
        help="the name of this experiment")
    parser.add_argument("--seed", type=int, default=1,
        help="seed of the experiment")
    parser.add_argument("--torch-deterministic", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="if toggled, `torch.backends.cudnn.deterministic=False`")
    parser.add_argument("--cuda", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="if toggled, cuda will be enabled by default")
    parser.add_argument("--track", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True,
        help="if toggled, this experiment will be tracked with Weights and Biases")
    parser.add_argument("--wandb-project-name", type=str, default="DrS",
        help="the wandb's project name")
    parser.add_argument("--wandb-entity", type=str, default=None,
        help="the entity (team) of wandb's project")
    parser.add_argument("--capture-video", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True,
        help="whether to capture videos of the agent performances (check out `videos` folder)")

    # Algorithm specific arguments
    parser.add_argument("--env-id", type=str, default="TurnFaucet_DrS_learn-v0",
        help="the id of the environment")
    parser.add_argument("--demo-path", type=str, default='demo_data/TurnFaucet_100.pkl',
        help="the path of demo file")
    parser.add_argument("--total-timesteps", type=int, default=2_000_000,
        help="total timesteps of the experiments")
    parser.add_argument("--buffer-size", type=int, default=None,
        help="the replay memory buffer size")
    parser.add_argument("--gamma", type=float, default=0.8,
        help="the discount factor gamma")
    parser.add_argument("--tau", type=float, default=0.005,
        help="target smoothing coefficient (default: 0.01)")
    parser.add_argument("--batch-size", type=int, default=1024,
        help="the batch size of sample from the reply memory")
    parser.add_argument("--learning-starts", type=int, default=4000,
        help="timestep to start learning")
    parser.add_argument("--policy-lr", type=float, default=3e-4,
        help="the learning rate of the policy network optimizer")
    parser.add_argument("--q-lr", type=float, default=3e-4,
        help="the learning rate of the Q network network optimizer")
    parser.add_argument("--disc-lr", type=float, default=3e-4,
        help="the learning rate of the discriminator optimizer")
    parser.add_argument("--policy-frequency", type=int, default=1,
        help="the frequency of training policy (delayed)")
    parser.add_argument("--target-network-frequency", type=int, default=1,
        help="the frequency of updates for the target nerworks")
    parser.add_argument("--disc-frequency", type=int, default=1,
        help="the frequency of training discriminator (delayed)")
    parser.add_argument("--disc-th", type=float, default=0.95,
        help="the success rate threshold for early stopping discriminator training")
    parser.add_argument("--alpha", type=float, default=0.2,
        help="Entropy regularization coefficient.")
    parser.add_argument("--autotune", type=lambda x:bool(strtobool(x)), default=True, nargs="?", const=True,
        help="automatic tuning of the entropy coefficient")
    parser.add_argument("--utd", type=float, default=0.5,
        help="Update-to-Data ratio (number of gradient updates / number of env steps)")
    
    parser.add_argument("--output-dir", type=str, default='output')
    parser.add_argument("--eval-freq", type=int, default=100_000)
    parser.add_argument("--num-envs", type=int, default=16)
    parser.add_argument("--num-eval-episodes", type=int, default=10)
    parser.add_argument("--num-eval-envs", type=int, default=1)
    parser.add_argument("--sync-venv", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True)
    parser.add_argument("--training-freq", type=int, default=64)
    parser.add_argument("--log-freq", type=int, default=10000)
    parser.add_argument("--num-demo-traj", type=int, default=None)
    parser.add_argument("--save-freq", type=int, default=2000000)
    parser.add_argument("--control-mode", type=str, default='pd_ee_delta_pose')
    parser.add_argument("--n-stages", type=int, required=True)

    args = parser.parse_args()
    args.algo_name = ALGO_NAME
    args.script = __file__
    if args.buffer_size is None:
        args.buffer_size = args.total_timesteps
    args.buffer_size = min(args.total_timesteps, args.buffer_size)
    args.num_eval_envs = min(args.num_eval_envs, args.num_eval_episodes)
    assert args.num_eval_episodes % args.num_eval_envs == 0
    assert args.training_freq % args.num_envs == 0
    assert (args.training_freq * args.utd).is_integer()
    # fmt: on
    return args

import drs.envs_with_stage_indicators
from mani_skill2.utils.wrappers import RecordEpisode

def make_env(env_id, seed, control_mode=None, video_dir=None, **kwargs):
    def thunk():
        env = gym.make(env_id, reward_mode='semi_sparse', control_mode=control_mode,
                       render_mode='cameras' if video_dir else None, **kwargs)
        if video_dir:
            env = RecordEpisode(env, output_dir=video_dir, save_trajectory=False, info_on_video=True)
        env = gym.wrappers.RecordEpisodeStatistics(env)
        env = gym.wrappers.ClipAction(env)

        env.action_space.seed(seed)
        env.observation_space.seed(seed)
        return env

    return thunk


# ALGO LOGIC: initialize agent here:
class SoftQNetwork(nn.Module):
    def __init__(self, env):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(np.array(env.single_observation_space.shape).prod() + np.prod(env.single_action_space.shape), 256),
            nn.ReLU(),
            nn.Linear(256, 256),
            nn.ReLU(),
            nn.Linear(256, 256),
            nn.ReLU(),
            nn.Linear(256, 1),
        )

    def forward(self, x, a):
        x = torch.cat([x, a], 1)
        return self.net(x)


LOG_STD_MAX = 2
LOG_STD_MIN = -5


class Actor(nn.Module):
    def __init__(self, env):
        super().__init__()
        self.backbone = nn.Sequential(
            nn.Linear(np.array(env.single_observation_space.shape).prod(), 256),
            nn.ReLU(),
            nn.Linear(256, 256),
            nn.ReLU(),
            nn.Linear(256, 256),
            nn.ReLU(),
        )
        self.fc_mean = nn.Linear(256, np.prod(env.single_action_space.shape))
        self.fc_logstd = nn.Linear(256, np.prod(env.single_action_space.shape))
        # action rescaling
        h, l = env.single_action_space.high, env.single_action_space.low
        self.register_buffer("action_scale", torch.tensor((h - l) / 2.0, dtype=torch.float32))
        self.register_buffer("action_bias", torch.tensor((h + l) / 2.0, dtype=torch.float32))
        # will be saved in the state_dict

    def forward(self, x):
        x = self.backbone(x)
        mean = self.fc_mean(x)
        log_std = self.fc_logstd(x)
        log_std = torch.tanh(log_std)
        log_std = LOG_STD_MIN + 0.5 * (LOG_STD_MAX - LOG_STD_MIN) * (log_std + 1)  # From SpinUp / Denis Yarats

        return mean, log_std

    def get_eval_action(self, x):
        x = self.backbone(x)
        mean = self.fc_mean(x)
        action = torch.tanh(mean) * self.action_scale + self.action_bias
        return action

    def get_action(self, x):
        mean, log_std = self(x)
        std = log_std.exp()
        normal = torch.distributions.Normal(mean, std)
        x_t = normal.rsample()  # for reparameterization trick (mean + std * N(0,1))
        y_t = torch.tanh(x_t)
        action = y_t * self.action_scale + self.action_bias
        log_prob = normal.log_prob(x_t)
        # Enforcing Action Bound
        log_prob -= torch.log(self.action_scale * (1 - y_t.pow(2)) + 1e-6)
        log_prob = log_prob.sum(1, keepdim=True)
        mean = torch.tanh(mean) * self.action_scale + self.action_bias
        return action, log_prob, mean

    def to(self, device):
        self.action_scale = self.action_scale.to(device)
        self.action_bias = self.action_bias.to(device)
        return super().to(device)

class Discriminator(nn.Module):
    def __init__(self, envs, n_stages):
        super().__init__()
        self.n_stages = n_stages
        state_shape = np.prod(envs.single_observation_space.shape)
        self.nets = nn.ModuleList([
            nn.Sequential(
                nn.Linear(state_shape, 32),
                nn.Sigmoid(),
                nn.Linear(32, 1),
            ) for _ in range(n_stages)
        ])
        self.trained = [False] * n_stages

    def set_trained(self, stage_idx):
        self.trained[stage_idx] = True

    def forward(self, next_s, stage_idx):
        net = self.nets[stage_idx]
        return net(next_s)

    def get_reward(self, next_s, stage_idx, success):
        with torch.no_grad():
            bs = next_s.shape[0]
            stage_idx = stage_idx.squeeze(-1)
            if not torch.is_tensor(success):
                success = torch.tensor(success, device=next_s.device)
                success = success.reshape(bs, 1)
            if self.n_stages == 1:
                assert stage_idx == success.squeeze(-1)
            
            stage_rewards = [
                torch.tanh(self(next_s, stage_idx=i)) if self.trained[i] else torch.zeros(bs, 1, device=next_s.device)
            for i in range(self.n_stages)]
            stage_rewards = torch.cat(stage_rewards + [torch.zeros(bs, 1, device=next_s.device)], dim=1)

            k = 3
            reward = k * stage_idx + stage_rewards[torch.arange(bs), stage_idx.long()]
            reward = reward / (k * self.n_stages) # reward is in (0, 1]
            reward = reward - 2 # make the reward negative
            #reward = reward + 1 # make the reward positive

            return reward

class DiscriminatorBuffer(object):
    # can be optimized by create a buffer of size (n_traj, len_traj, dim)
    def __init__(self, buffer_size, obs_space, action_space, device):
        self.buffer_size = buffer_size
        self.next_observations = np.zeros((self.buffer_size,) + obs_space.shape, dtype=obs_space.dtype)
        self.device = device
        self.pos = 0
        self.full = False

    @property
    def size(self) -> int:
        return self.buffer_size if self.full else self.pos

    def add(self, next_obs):
        l = next_obs.shape[0]
        
        while self.pos + l >= self.buffer_size:
            self.full = True
            k = self.buffer_size - self.pos
            self.next_observations[self.pos:] = next_obs[:k]
            self.pos = 0
            next_obs = next_obs[k:]
            l = next_obs.shape[0]
            
        self.next_observations[self.pos:self.pos+l] = next_obs.copy()
        self.pos = (self.pos + l) % self.buffer_size

    def sample(self, batch_size):
        idxs = np.random.randint(0, self.size, size=batch_size)
        batch = dict(
            next_observations=self.next_observations[idxs],
        )
        return {k: torch.tensor(v).to(self.device) for k,v in batch.items()}

def sample_from_multi_buffers(buffers, batch_size):
    # Warning: when the buffers are full, this will make samples not uniform
    sizes = [b.size for b in buffers]
    tot_size = sum(sizes)
    if tot_size == 0:
        raise Exception('All buffers are empty!')
    n_samples = [int(s / tot_size * batch_size) for s in sizes]
    if sum(n_samples) < batch_size:
        n_samples[np.argmax(sizes)] += batch_size - sum(n_samples)
    batches = []
    for b, n in zip(buffers, n_samples):
        if n > 0:
            if b.size == 0:
                raise Exception('Buffer is empty!')
            batches.append(b.sample(n))
    ret = {}
    for k in batches[0].keys():
        ret[k] = torch.cat([b[k] for b in batches], dim=0)
    return ret


def collect_episode_info(infos, result=None):
    if result is None:
        result = defaultdict(list)
    if "final_info" in infos: # infos is a dict
        indices = np.where(infos["_final_info"])[0] # not all envs are done at the same time
        for i in indices:
            info = infos["final_info"][i] # info is also a dict
            ep = info['episode']
            print(f"global_step={global_step}, ep_return={ep['r'][0]:.2f}, ep_len={ep['l'][0]}, success={info['success']}")
            result['return'].append(ep['r'][0])
            result['len'].append(ep["l"][0])
            result['success'].append(info['success'])
    return result

def evaluate(n, agent, eval_envs, device):
    print('======= Evaluation Starts =========')
    agent.eval()
    result = defaultdict(list)
    obs, info = eval_envs.reset() # don't seed here
    while len(result['return']) < n:
        with torch.no_grad():
            action = agent.get_eval_action(torch.Tensor(obs).to(device))
        obs, rew, terminated, truncated, info = eval_envs.step(action.cpu().numpy())
        collect_episode_info(info, result)
    print('======= Evaluation Ends =========')
    agent.train()
    return result


if __name__ == "__main__":
    args = parse_args()

    now = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
    tag = '{:s}_{:d}'.format(now, args.seed)
    if args.exp_name: tag += '_' + args.exp_name
    log_name = os.path.join(args.env_id, ALGO_NAME, tag)
    log_path = os.path.join(args.output_dir, log_name)

    if args.track:
        import wandb

        wandb.init(
            project=args.wandb_project_name,
            entity=args.wandb_entity,
            sync_tensorboard=True,
            config=vars(args),
            name=log_name.replace(os.path.sep, "__"),
            monitor_gym=True,
            save_code=True,
        )
    writer = SummaryWriter(log_path)
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s" % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )
    import json
    with open(f'{log_path}/args.json', 'w') as f:
        json.dump(vars(args), f, indent=4)

    # TRY NOT TO MODIFY: seeding
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = args.torch_deterministic

    device = torch.device("cuda" if torch.cuda.is_available() and args.cuda else "cpu")

    # env setup
    VecEnv = gym.vector.SyncVectorEnv if args.sync_venv or args.num_envs == 1 \
        else lambda x: gym.vector.AsyncVectorEnv(x, context='forkserver')
    envs = VecEnv(
        [make_env(args.env_id, args.seed + i, args.control_mode) for i in range(args.num_envs)]
    )
    VecEnv = gym.vector.SyncVectorEnv if args.sync_venv or args.num_eval_envs == 1 \
        else lambda x: gym.vector.AsyncVectorEnv(x, context='forkserver')
    eval_envs = VecEnv(
        [make_env(args.env_id, args.seed + 1000 + i, args.control_mode,
                f'{log_path}/videos' if args.capture_video and i == 0 else None,
        ) 
        for i in range(args.num_eval_envs)]
    )
    eval_envs.reset(seed=args.seed+1000) # seed eval_envs here, and no more seeding during evaluation
    assert isinstance(envs.single_action_space, gym.spaces.Box), "only continuous action space is supported"
    max_action = float(envs.single_action_space.high[0])

    # demo dataset setup
    if args.demo_path:
        from drs.data_utils import load_demo_dataset
        demo_dataset = load_demo_dataset(args.demo_path, keys=['next_observations'])
        demo_size = list(demo_dataset.values())[0].shape[0]

    # discriminator setup
    disc = Discriminator(envs, args.n_stages).to(device)
    disc_optimizer = optim.Adam(disc.parameters(), lr=args.disc_lr)
    disc_training = [True] * args.n_stages

    # agent setup
    actor = Actor(envs).to(device)
    qf1 = SoftQNetwork(envs).to(device)
    qf2 = SoftQNetwork(envs).to(device)
    qf1_target = SoftQNetwork(envs).to(device)
    qf2_target = SoftQNetwork(envs).to(device)
    qf1_target.load_state_dict(qf1.state_dict())
    qf2_target.load_state_dict(qf2.state_dict())
    q_optimizer = optim.Adam(list(qf1.parameters()) + list(qf2.parameters()), lr=args.q_lr)
    actor_optimizer = optim.Adam(list(actor.parameters()), lr=args.policy_lr)

    # Automatic entropy tuning
    if args.autotune:
        target_entropy = -torch.prod(torch.Tensor(envs.single_action_space.shape).to(device)).item()
        log_alpha = torch.zeros(1, requires_grad=True, device=device)
        alpha = log_alpha.exp().item()
        a_optimizer = optim.Adam([log_alpha], lr=args.q_lr)
    else:
        alpha = args.alpha

    envs.single_observation_space.dtype = np.float32
    rb = ReplayBuffer(
        args.buffer_size,
        envs.single_observation_space,
        envs.single_action_space,
        device,
        n_envs=args.num_envs,
        handle_timeout_termination=False, # stable-baselines3 has not fully supported Gymnasium's termination signal
    )

    # DrS specific
    stage_buffers = [DiscriminatorBuffer(
        args.buffer_size,
        envs.single_observation_space,
        envs.single_action_space,
        device,
    ) for _ in range(args.n_stages + 1)]
    if args.demo_path:
        stage_buffers[-1].add(next_obs=demo_dataset['next_observations'])

    tmp_env = make_env(args.env_id, seed=0)()
    max_t = tmp_env.spec.max_episode_steps
    del tmp_env
    assert args.learning_starts > args.num_envs * max_t, "learning_starts must be larger than num_envs * max_ep_steps"
    episode_next_obs = np.zeros((args.num_envs, max_t) + envs.single_observation_space.shape)
    step_in_episodes = np.zeros((args.num_envs,1,1), dtype=np.int32)

    # TRY NOT TO MODIFY: start the game
    start_time = time.time()
    obs, info = envs.reset(seed=args.seed) # in Gymnasium, seed is given to reset() instead of seed()
    global_step = 0
    global_update = 0
    learning_has_started = False
    num_updates_per_training = int(args.training_freq * args.utd)
    result = defaultdict(list)

    while global_step < args.total_timesteps:

        #############################################
        # Interact with environments
        #############################################
        for local_step in range(args.training_freq // args.num_envs):
            global_step += 1 * args.num_envs

            # ALGO LOGIC: put action logic here
            if not learning_has_started:
                actions = np.array([envs.single_action_space.sample() for _ in range(envs.num_envs)])
            else:
                actions, _, _ = actor.get_action(torch.Tensor(obs).to(device))
                actions = actions.detach().cpu().numpy()

            # TRY NOT TO MODIFY: execute the game and log data.
            next_obs, rewards, terminations, truncations, infos = envs.step(actions)
            success_rewards = terminations.astype(rewards.dtype)

            # TRY NOT TO MODIFY: record rewards for plotting purposes
            result = collect_episode_info(infos, result)

            # TRY NOT TO MODIFY: save data to reply buffer; handle `final_observation`
            real_next_obs = next_obs.copy()
            # bootstrap at truncated
            need_final_obs = truncations & (~terminations) # only need final obs when truncated and not terminated
            stop_bootstrap = terminations # only stop bootstrap when terminated, don't stop when truncated
            for idx, _need_final_obs in enumerate(need_final_obs):
                if _need_final_obs:
                    real_next_obs[idx] = infos["final_observation"][idx]
            rb.add(obs, real_next_obs, actions, rewards, stop_bootstrap, infos)

            # DrS pecific: record data for the current episode, add data to stage buffers
            np.put_along_axis(episode_next_obs, step_in_episodes, values=real_next_obs[:, None, :], axis=1)
            step_in_episodes += 1

            for i, d in enumerate(terminations | truncations):
                if d:                    
                    # add completed trajectory to corresponding buffer
                    l = step_in_episodes[i,0,0]
                    traj = episode_next_obs[i, :l]
                    if infos["final_info"][i]['success']:
                        stage_idx = args.n_stages
                    elif args.n_stages > 1:
                        stage_indices = traj[:, -(args.n_stages-1):].sum(axis=1)
                        best_step = l -1 - np.argmax(stage_indices[::-1])
                        stage_idx = int(stage_indices[best_step])
                        traj = traj[:best_step+1]
                    else:
                        stage_idx = 0
                    stage_buffers[stage_idx].add(traj)
                    step_in_episodes[i] = 0

                    for j in range(1, args.n_stages):
                        result[f'stage_{j}_success'].append(j<=stage_idx)

            # TRY NOT TO MODIFY: CRUCIAL step easy to overlook
            obs = next_obs

        # ALGO LOGIC: training.
        if global_step < args.learning_starts:
            continue

        learning_has_started = True
        for local_update in range(num_updates_per_training):
            global_update += 1
            data = rb.sample(args.batch_size)

            #############################################
            # Train discriminator
            #############################################
            if global_update % args.disc_frequency == 0:
                for stage_idx in range(args.n_stages):
                    if not disc_training[stage_idx]:
                        continue
                    success_data = sample_from_multi_buffers(stage_buffers[stage_idx+1:], args.batch_size)
                    if not success_data:
                        break
                    fail_data = sample_from_multi_buffers(stage_buffers[:stage_idx+1], args.batch_size)

                    disc_next_obs = torch.cat([fail_data['next_observations'], success_data['next_observations']], dim=0)
                    disc_labels = torch.cat([
                        torch.zeros((args.batch_size, 1), device=device), # fail label is 0
                        torch.ones((args.batch_size, 1), device=device), # success label is 1
                    ], dim=0)

                    logits = disc(disc_next_obs, stage_idx)
                    disc_loss = F.binary_cross_entropy_with_logits(logits, disc_labels)
                    
                    disc_optimizer.zero_grad()
                    disc_loss.backward()
                    disc_optimizer.step()

                    pred = logits.detach() > 0

                    disc.set_trained(stage_idx)

            #############################################
            # Train agent
            #############################################
            
            # compute reward by discriminator
            disc_rewards = disc.get_reward(data.next_observations, data.rewards, data.dones)

            # update the value networks
            with torch.no_grad():
                next_state_actions, next_state_log_pi, _ = actor.get_action(data.next_observations)
                qf1_next_target = qf1_target(data.next_observations, next_state_actions)
                qf2_next_target = qf2_target(data.next_observations, next_state_actions)
                min_qf_next_target = torch.min(qf1_next_target, qf2_next_target) - alpha * next_state_log_pi
                next_q_value = disc_rewards.flatten() + (1 - data.dones.flatten()) * args.gamma * (min_qf_next_target).view(-1)
                # data.dones is "stop_bootstrap", which is computed earlier

            qf1_a_values = qf1(data.observations, data.actions).view(-1)
            qf2_a_values = qf2(data.observations, data.actions).view(-1)
            qf1_loss = F.mse_loss(qf1_a_values, next_q_value)
            qf2_loss = F.mse_loss(qf2_a_values, next_q_value)
            qf_loss = qf1_loss + qf2_loss

            q_optimizer.zero_grad()
            qf_loss.backward()
            q_optimizer.step()

            # update the policy network
            if global_update % args.policy_frequency == 0:  # TD 3 Delayed update support
                pi, log_pi, _ = actor.get_action(data.observations)
                qf1_pi = qf1(data.observations, pi)
                qf2_pi = qf2(data.observations, pi)
                min_qf_pi = torch.min(qf1_pi, qf2_pi)
                actor_loss = ((alpha * log_pi) - min_qf_pi).mean()

                actor_optimizer.zero_grad()
                actor_loss.backward()
                actor_optimizer.step()

                if args.autotune:
                    with torch.no_grad():
                        _, log_pi, _ = actor.get_action(data.observations)
                    alpha_loss = (-log_alpha * (log_pi + target_entropy)).mean()

                    a_optimizer.zero_grad()
                    alpha_loss.backward()
                    a_optimizer.step()
                    alpha = log_alpha.exp().item()

            # update the target networks
            if global_update % args.target_network_frequency == 0:
                for param, target_param in zip(qf1.parameters(), qf1_target.parameters()):
                    target_param.data.copy_(args.tau * param.data + (1 - args.tau) * target_param.data)
                for param, target_param in zip(qf2.parameters(), qf2_target.parameters()):
                    target_param.data.copy_(args.tau * param.data + (1 - args.tau) * target_param.data)

        # Log training-related data
        if (global_step - args.training_freq) // args.log_freq < global_step // args.log_freq:
            if len(result['return']) > 0:
                for k, v in result.items():
                    tag = k if '/' in k else f"train/{k}"
                    writer.add_scalar(tag, np.mean(v), global_step)
                for j in range(1, args.n_stages):
                    if np.mean(result[f'stage_{j}_success']) > args.disc_th:
                        disc_training[j-1] = False
                sr = np.mean(result['success'])
                if sr > args.disc_th:
                    disc_training[-1] = False
                result = defaultdict(list)
            writer.add_scalar("losses/qf1_values", qf1_a_values.mean().item(), global_step)
            writer.add_scalar("losses/qf2_values", qf2_a_values.mean().item(), global_step)
            writer.add_scalar("losses/qf1_loss", qf1_loss.item(), global_step)
            writer.add_scalar("losses/qf2_loss", qf2_loss.item(), global_step)
            writer.add_scalar("losses/qf_loss", qf_loss.item() / 2.0, global_step)
            writer.add_scalar("losses/actor_loss", actor_loss.item(), global_step)
            writer.add_scalar("losses/alpha", alpha, global_step)
            writer.add_scalar("charts/SPS", int(global_step / (time.time() - start_time)), global_step)
            if args.autotune:
                writer.add_scalar("losses/alpha_loss", alpha_loss.item(), global_step)

        # Evaluation
        if (global_step - args.training_freq) // args.eval_freq < global_step // args.eval_freq:
            result = evaluate(args.num_eval_episodes, actor, eval_envs, device)
            for k, v in result.items():
                writer.add_scalar(f"eval/{k}", np.mean(v), global_step)

        # Checkpoint
        if args.save_freq and ( global_step >= args.total_timesteps or \
                (global_step - args.training_freq) // args.save_freq < global_step // args.save_freq):
            os.makedirs(f'{log_path}/checkpoints', exist_ok=True)
            torch.save({
                'discriminator': disc.state_dict(),
            }, f'{log_path}/checkpoints/{global_step}.pt')

    envs.close()
    writer.close()
