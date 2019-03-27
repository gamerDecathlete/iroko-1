from __future__ import print_function
import argparse
import os
import random
import logging
import time
import json

# Ray imports
import ray
from ray.rllib.agents.registry import get_agent_class
from ray.rllib.agents.agent import Agent, with_common_config
from ray.tune.registry import register_env
import ray.tune as tune
from ray.tune.schedulers import PopulationBasedTraining
# Iroko imports
import dc_gym
from dc_gym.factories import EnvFactory

# set up paths
cwd = os.getcwd()
lib_dir = os.path.dirname(dc_gym.__file__)
INPUT_DIR = lib_dir + '/inputs'
OUTPUT_DIR = cwd + '/results'

PARSER = argparse.ArgumentParser()
PARSER.add_argument('--env', '-e', dest='env',
                    default='iroko', help='The platform to run.')
PARSER.add_argument('--topo', dest='topo',
                    default='dumbbell', help='The topology to operate on.')
PARSER.add_argument('--num_hosts', dest='num_hosts',
                    default='4', help='The number of hosts in the topology.')
PARSER.add_argument('--agent', '-a', dest='agent', default="PG",
                    help='must be string of either: PPO, DDPG, PG,'
                         ' DCTCP, TCP_NV, PCC, or TCP', type=str.lower)
PARSER.add_argument('--timesteps', '-t', dest='timesteps',
                    type=int, default=10000,
                    help='total number of timesteps to train rl agent, '
                         'if tune specified is wall clock time')
PARSER.add_argument('--pattern', '-p', dest='pattern_index',
                    type=int, default=0,
                    help='Traffic pattern we are testing.')
PARSER.add_argument('--checkpoint_freq', '-cf', dest='checkpoint_freq',
                    type=int, default=0,
                    help='how often to checkpoint model')
PARSER.add_argument('--restore', '-r', dest='restore', default=None,
                    help='Path to checkpoint to restore (for testing), must '
                    'end like this: <path>/checkpoint-* where star is the '
                    'check point number')
PARSER.add_argument('--output', dest='output_dir', default=OUTPUT_DIR,
                    help='Folder which contains all the collected metrics.')
PARSER.add_argument('--transport', dest='transport', default="udp",
                    help='Choose the transport protocol of the hosts.')
PARSER.add_argument('--tune', action="store_true", default=False,
                    help='Specify whether to perform hyperparameter tuning')
ARGS = PARSER.parse_args()


class MaxAgent(Agent):
    """Agent that always takes the maximum available action."""
    _agent_name = "MaxAgent"
    _default_config = with_common_config({})

    def _init(self):
        self.env = self.env_creator(self.config["env_config"])
        self.env.reset()

    def _train(self):
        steps = 0
        done = False
        reward = 0.0
        while not done:
            action = self.env.action_space.high
            obs, r, done, info = self.env.step(action)
            reward += r
            steps += 1
            if steps >= self.config["env_config"]["iterations"]:
                done = True
        return {
            "episode_reward_mean": reward,
            "timesteps_this_iter": steps,
        }


class RandomAgent(Agent):
    """Agent that always takes the maximum available action."""
    _agent_name = "RandomAgent"
    _default_config = with_common_config({})

    def _init(self):
        self.env = self.env_creator(self.config["env_config"])
        self.env.reset()

    def _train(self):
        steps = 0
        done = False
        reward = 0.0
        while not done:
            action = self.env.action_space.sample()
            obs, r, done, info = self.env.step(action)
            reward += r
            steps += 1
            if steps >= self.config["env_config"]["iterations"]:
                done = True
        return {
            "episode_reward_mean": reward,
            "timesteps_this_iter": steps,
        }


def check_dir(directory):
    # create the folder if it does not exit
    if not directory == '' and not os.path.exists(directory):
        print("Folder %s does not exist! Creating..." % directory)
        os.makedirs(directory)


def get_env(env_config):
    return EnvFactory.create(env_config)


def set_tuning_parameters(agent, config):
    scheduler = None
    if agent.lower() == "ppo":
        # Postprocess the perturbed config to ensure it's still valid
        def explore(config):
            # ensure we collect enough timesteps to do sgd
            config["train_batch_size"] = max(config["train_batch_size"], 4) #should be 4 at minimum
            if config["train_batch_size"] < config["sgd_minibatch_size"] * 2:
                config["train_batch_size"] = config["sgd_minibatch_size"] * 2
            
            # ensure we run at least one sgd iter
            if config["num_sgd_iter"] < 1:
                config["num_sgd_iter"] = 1
            if config['horizon'] < 32:
                config['horizon'] = 32
            for k in config.keys():
                if k == 'use_gae':
                    continue #that one is fine and also non numeric
                if config[k] < 0.0:
                    config[k] = 0.0 #this...is a lazy way to make sure things are at worse 0
            return config
        #mutation distributions
        hyper_params = {
            #update frequency
            "horizon": lambda : random.randint(32, 5000),
            "train_batch_size": lambda: random.randint(4,  4096),
            "num_sgd_iter": lambda: random.randint(3, 30),
            #Objective hyperparams:
            'clip_param': lambda: random.choice([0.1, 0.2, 0.3]),
            'kl_target': lambda: random.uniform(0.003, 0.03),
            'kl_coeff': lambda: random.uniform(0.3, 1),
            'use_gae': lambda:random.choice([True, False]),
            'gamma': lambda: random.choice([0.99, random.uniform(0.8, 0.9997), random.uniform(0.8, 0.9997)]),
            'lambda': lambda: random.uniform(0.9, 1.0),

            #val fn & entropy coeff
            'vf_loss_coeff': lambda: random.choice([0.5, 1.0]),
            'entropy_coeff': lambda: random.uniform(0, 0.01),
            'sgd_stepsize': lambda: random.uniform(5e-6, 0.003),

        }
        #creates a wide range of the potential population
        for k in hyper_params.keys():
            config[k] = tune.sample_from(lambda spec: hyper_params[k])

        scheduler = PopulationBasedTraining(time_attr="time_total_s",
                                            reward_attr="episode_reward_mean",
                                            perturbation_interval=120,
                                            resample_probability=0.80,
                                            hyperparam_mutations=hyper_params,
                                            custom_explore_fn=explore)
                                           
    if agent.lower() == "ddpg":
        pass

    if agent.lower() == "pg":
        pass

    return config, scheduler


def clean():
    ''' A big fat hammer to get rid of all the debris left over by ray '''
    print("Removing all previous traces of Mininet and ray")
    ray_kill = "sudo kill -9 $(ps aux | grep 'ray' | awk '{print $2}')"
    os.system(ray_kill)
    os.system('sudo mn -c')
    os.system("sudo killall -9 goben")
    os.system("sudo killall -9 node_control")


def get_agent(agent_name):

    if agent_name.lower() == "rnd":
        agent_class = type(agent_name.upper(), (RandomAgent,), {})
        return agent_class
    try:
        agent_class = get_agent_class(agent_name.upper())
    except Exception as e:
        print("%s Loading basic algorithm" % e)
        # We use PG as the base class for experiments
        agent_class = type(agent_name.upper(), (MaxAgent,), {})
    return agent_class


def get_tune_experiment(config, agent):
    SCHEDULE = False
    scheduler = None
    name = "%s_tune" % agent
    agent_class = get_agent(agent)

    experiment = {
        name: {
            'run': agent_class,
            'local_dir': ARGS.output_dir,
            "stop": {"timesteps_total": ARGS.timesteps},
            "env": "dc_env",
            "checkpoint_freq": ARGS.checkpoint_freq,
            "checkpoint_at_end": True,
            "restore": ARGS.restore,
        }
    }

    if SCHEDULE:
        experiment[name]["stop"] = {"time_total_s": ARGS.timesteps / 2}
        experiment[name]["num_samples"] = 2
        # custom changes to experiment
        print("Performing tune experiment")
        config, scheduler = set_tuning_parameters(agent, config)
    config["env_config"]["topo_conf"] = {}
    config["env_config"]["topo_conf"]["parallel_envs"] = True
    experiment[name]["config"] = config
    return experiment, scheduler


def configure_ray(agent):
    # Load the config specific to the agent
    try:
        with open("%s/ray_configs/%s.json" % (cwd, ARGS.agent), 'r') as fp:
            config = json.load(fp)
    except IOError:
        # File does not exist, just initialize an empty configuration.
        print("Agent configuration does not exist, starting with default.")
        config = {}
    # Add the dynamic environment configuration
    config["clip_actions"] = True
    config["num_workers"] = 1
    config["num_gpus"] = 0
    config["batch_mode"] = "truncate_episodes"
    config["log_level"] = "ERROR"
    config["env_config"] = {
        "input_dir": INPUT_DIR,
        "output_dir": ARGS.output_dir + "/" + ARGS.agent,
        "env": ARGS.env,
        "topo": ARGS.topo,
        "agent": ARGS.agent,
        "transport": ARGS.transport,
        "iterations": ARGS.timesteps,
        "tf_index": ARGS.pattern_index,
    }
    if ARGS.timesteps > 50000:
        config["env_config"]["sample_delta"] = ARGS.timesteps / 50000

    return config


def run(config):
    agent_class = get_agent(config["env_config"]["agent"])
    config["horizon"] = ARGS.timesteps
    agent = agent_class(config=config, env="dc_env")
    agent.train()
    print('Generator Finished. Simulation over. Clearing dc_env...')


def tune_run(config):
    agent = config['env_config']['agent']
    experiment, scheduler = get_tune_experiment(config, agent)
    tune.run_experiments(experiment, scheduler=scheduler)


def init():
    check_dir(ARGS.output_dir + "/" + ARGS.agent)
    print("Registering the DC environment...")
    register_env("dc_env", get_env)
    print("Starting Ray...")
    ray.init(num_cpus=2, logging_level=logging.WARN)

    config = configure_ray(ARGS.agent)
    print("Starting experiment.")
    # Basic ray train currently does not work, always use tune for now
    if ARGS.tune:
        tune_run(config)
    else:
        run(config)
    # Wait until the topology is torn down completely
    time.sleep(10)
    print("Experiment has completed.")


if __name__ == '__main__':
    init()
