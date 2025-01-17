import argparse
import numpy as np
import tensorflow as tf
import time
import pickle
import sys
import os

sys.path.append('../')
sys.path.append('../../')
sys.path.append('../../../')

import maddpg.common.tf_util as U
from maddpg.trainer.m3ddpg import M3DDPGAgentTrainer
import tensorflow.contrib.layers as layers
from maddpg.trainer.m3ddpg import TruncatedNormal

def parse_args():
    parser = argparse.ArgumentParser("Reinforcement Learning experiments for multiagent environments")
    # Environment
    parser.add_argument("--scenario", type=str, default="simple", help="name of the scenario script")
    parser.add_argument("--max-episode-len", type=int, default=25, help="maximum episode length")
    parser.add_argument("--num-episodes", type=int, default=2000, help="number of episodes")
    parser.add_argument("--num-adversaries", type=int, default=0, help="number of adversaries")
    parser.add_argument("--good-policy", type=str, default="mmmaddpg", help="policy for good agents")
    parser.add_argument("--bad-policy", type=str, default="mmmaddpg", help="policy of adversaries")
    # Core training parameters
    parser.add_argument("--lr", type=float, default=1e-2, help="learning rate for Adam optimizer")
    parser.add_argument("--gamma", type=float, default=0.95, help="discount factor")
    parser.add_argument("--batch-size", type=int, default=1024, help="number of episodes to optimize at the same time")
    parser.add_argument("--num-units", type=int, default=64, help="number of units in the mlp")
    parser.add_argument("--adv-eps", type=float, default=1e-3, help="adversarial training rate")
    parser.add_argument("--adv-eps-s", type=float, default=1e-5, help="small adversarial training rate")
    parser.add_argument("--gpu-frac", type=float, default=0.3, help="Fraction of GPU memory usage.")
    # Checkpointing
    parser.add_argument("--exp-name", type=str, default=None, help="name of the experiment")
    parser.add_argument("--save-dir", type=str, default="/tmp/policy/", help="directory in which training state and model should be saved")
    parser.add_argument("--save-rate", type=int, default=1000, help="save model once every time this many episodes are completed")
    parser.add_argument("--load-name", type=str, default="", help="name of which training state and model are loaded, leave blank to load seperately")
    parser.add_argument("--load-good", type=str, default="", help="which good policy to load")
    parser.add_argument("--load-bad", type=str, default="", help="which bad policy to load")
    # Evaluation
    parser.add_argument("--test", action="store_true", default=False)
    parser.add_argument("--restore", action="store_true", default=False)
    parser.add_argument("--display", action="store_true", default=False)
    parser.add_argument("--benchmark", action="store_true", default=False)
    parser.add_argument("--benchmark-iters", type=int, default=100000, help="number of iterations run for benchmarking")
    parser.add_argument("--benchmark-dir", type=str, default="./benchmark_files/", help="directory where benchmark data is saved")
    parser.add_argument("--plots-dir", type=str, default="./learning_curves/", help="directory where plot data is saved")
    # Add Noise for observation 
    parser.add_argument("--noise-type", type=int, default=0, help="type can be: 0-none, 1-truncated normal distribution, 2-normal distribution")
    parser.add_argument('--noise-std', type=float, default=1.0, help='{0.0, 1.0, 2.0, 3.0, ...}, noise standard deviation')
    parser.add_argument("--d-value", type=float, default=1.0, help="a radius denoting how large the perturbation set is")
    return parser.parse_args()

def mlp_model(input, num_outputs, scope, reuse=False, num_units=64, rnn_cell=None):
    # This model takes as input an observation and returns values of all actions
    with tf.variable_scope(scope, reuse=reuse):
        out = input
        out = layers.fully_connected(out, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = layers.fully_connected(out, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = layers.fully_connected(out, num_outputs=num_outputs, activation_fn=None)
        return out

def make_env(scenario_name, arglist, benchmark=False):
    from multiagent.environment import MultiAgentEnv
    import multiagent.scenarios as scenarios

    # load scenario from script
    scenario = scenarios.load(scenario_name + ".py").Scenario()
    # create world
    world = scenario.make_world()
    # create multiagent environment
    if benchmark:
        env = MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation, scenario.benchmark_data)
    else:
        env = MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation)
    return env

def get_trainers(env, num_adversaries, obs_shape_n, arglist):
    trainers = []
    model = mlp_model
    trainer = M3DDPGAgentTrainer
    for i in range(num_adversaries):
        print("{} bad agents".format(i))
        policy_name = arglist.bad_policy
        trainers.append(trainer(
            "agent_%d" % i, model, obs_shape_n, env.observation_space, env.action_space, i, arglist,
            policy_name == 'ddpg', policy_name, policy_name == 'mmmaddpg'))
    for i in range(num_adversaries, env.n):
        print("{} good agents".format(i))
        policy_name = arglist.good_policy
        trainers.append(trainer(
            "agent_%d" % i, model, obs_shape_n, env.observation_space, env.action_space, i, arglist,
            policy_name == 'ddpg', policy_name, policy_name == 'mmmaddpg'))
    return trainers

def addNoise(origin_obs, X, d_value, n):
    obs_array = []
    for i in range(n):
        temp = np.array(origin_obs[i])
        noise = X.rvs(np.size(temp)) * d_value
        noise.shape = temp.shape
        obs_array.append((temp+noise).tolist())
    return obs_array

def train(arglist):
    np.random.seed(71)
    with U.single_threaded_session(arglist.gpu_frac):
        # Create environment
        env = make_env(arglist.scenario, arglist, arglist.benchmark)
        # Create agent trainers
        obs_shape_n = [env.observation_space[i].shape for i in range(env.n)]
        num_adversaries = min(env.n, arglist.num_adversaries)
        trainers = get_trainers(env, num_adversaries, obs_shape_n, arglist)
        print('Using good policy {} and bad policy {} with {} adversaries'.format(arglist.good_policy, arglist.bad_policy, num_adversaries))
        X = TruncatedNormal(std = arglist.noise_std)

        # Initialize
        U.initialize()

        # Load previous results, if necessary
        arglist.load_name = arglist.save_dir
        print('Loading previous state from {}'.format(arglist.load_name))
        U.load_state(arglist.load_name)

        episode_rewards = [0.0]  # sum of rewards for all agents
        agent_rewards = [[0.0] for _ in range(env.n)]  # individual agent reward
        final_ep_rewards = []  # sum of rewards for training curve
        final_ep_ag_rewards = []  # agent rewards for training curve
        agent_info = [[[]]]  # placeholder for benchmarking info
        saver = tf.train.Saver()
        obs_n = env.reset()
        episode_step = 0
        train_step = 0
        t_start = time.time()

        print('Starting iterations...')
        while True:
            # get action
            if arglist.noise_type != 0:
                obs_n = addNoise(obs_n, X, arglist.d_value, env.n)
            action_n = [agent.action(obs) for agent, obs in zip(trainers,obs_n)]
            # environment step
            new_obs_n, rew_n, done_n, info_n = env.step(action_n)
            episode_step += 1
            done = all(done_n)
            terminal = (episode_step >= arglist.max_episode_len)
            obs_n = new_obs_n

            for i, rew in enumerate(rew_n):
                episode_rewards[-1] += rew
                agent_rewards[i][-1] += rew

            if done or terminal:
                obs_n = env.reset()
                episode_step = 0
                episode_rewards.append(0)
                for a in agent_rewards:
                    a.append(0)
                agent_info.append([[]])

            # increment global step counter
            train_step += 1

            # for displaying learned policies
            if arglist.display:
                time.sleep(0.1)
                env.render()
                continue

            # save model, display training output
            if terminal and (len(episode_rewards) % arglist.save_rate == 0):
                # print statement depends on whether or not there are adversaries
                if num_adversaries == 0:
                    print("episodes: {}, mean episode reward: {}, std deviation: {}, time: {}".format(
                        len(episode_rewards), np.mean(episode_rewards), np.std(episode_rewards), round(time.time()-t_start, 3)))
                else:
                    print("episodes: {}, mean episode reward: {}, std deviation: {}, agent episode reward: {}, agent std deviation: {}, time: {}".format(
                        len(episode_rewards), np.mean(episode_rewards), np.std(episode_rewards),
                        [np.mean(rew) for rew in agent_rewards],  [np.std(rew) for rew in agent_rewards], round(time.time()-t_start, 3)))
                t_start = time.time()

            # saves final episode reward for plotting training curve later
            if len(episode_rewards) >= arglist.num_episodes:
                print("episode_rewards: ")
                for i in range(len(episode_rewards)):
                    print(episode_rewards[i])
                break

if __name__ == '__main__':
    arglist = parse_args()
    train(arglist)
