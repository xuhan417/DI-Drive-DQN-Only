import os
import argparse
import numpy as np
from functools import partial
from easydict import EasyDict
import copy
from tensorboardX import SummaryWriter

from core.envs import SimpleCarlaEnv, BenchmarkEnvWrapper
from core.utils.others.tcp_helper import parse_carla_tcp

from ding.envs import SyncSubprocessEnvManager, BaseEnvManager
from ding.policy import DQNPolicy, PPOPolicy, TD3Policy, SACPolicy, DDPGPolicy
from ding.worker import BaseLearner, SampleSerialCollector, AdvancedReplayBuffer, NaiveReplayBuffer
from ding.utils import set_pkg_seed
from ding.rl_utils import get_epsilon_greedy_fn

from demo.simple_rl.model import DQNRLModel
from demo.simple_rl.env_wrapper import DiscreteEnvWrapper
from core.utils.others.ding_utils import compile_config
from core.utils.others.ding_utils import read_ding_config


def wrapped_discrete_env(env_cfg, wrapper_cfg, host, port, tm_port=None):
    env = SimpleCarlaEnv(env_cfg, host, port, tm_port)
    return BenchmarkEnvWrapper(DiscreteEnvWrapper(env), wrapper_cfg)
    # return DiscreteEnvWrapper(env)


def get_cfg(args):
    if args.ding_cfg is not None:
        ding_cfg = args.ding_cfg
    else:
        ding_cfg = {
            'dqn': 'demo.simple_rl.config.dqn_config.py'
        }[args.policy]
    default_train_config = read_ding_config(ding_cfg)
    default_train_config.exp_name = args.name
    use_policy, _ = get_cls(args.policy)
    use_buffer = AdvancedReplayBuffer if args.policy != 'ppo' else None
    cfg = compile_config(
        cfg = default_train_config,
        env_manager = SyncSubprocessEnvManager,
        policy = use_policy,
        learner = BaseLearner,
        collector = SampleSerialCollector,
        buffer = use_buffer,
    )
    return cfg

def get_cls(spec):
    policy_cls, model_cls = {
        'dqn': (DQNPolicy, DQNRLModel)
    }[spec]

    return policy_cls, model_cls


def main(args, seed=0):
    cfg = get_cfg(args)
    tcp_list = parse_carla_tcp(cfg.server)
    collector_env_num, evaluator_env_num = cfg.env.collector_env_num, cfg.env.evaluator_env_num
    assert len(tcp_list) >= collector_env_num + evaluator_env_num, \
        "Carla server not enough! Need {} servers but only found {}.".format(
            collector_env_num + evaluator_env_num, len(tcp_list)
    )
   
    wrapped_env = wrapped_discrete_env

    collector_env = SyncSubprocessEnvManager(
        
        env_fn=[partial(wrapped_env, cfg.env, cfg.env.wrapper.collect, *tcp_list[i]) for i in range(collector_env_num)],
        
        cfg=cfg.env.manager.collect,
    )
    # evaluate_env = BaseEnvManager(
    #     env_fn=[partial(wrapped_env, cfg.env, cfg.env.wrapper.eval, *tcp_list[collector_env_num + i]) for i in range(evaluator_env_num)],
    #     cfg=cfg.env.manager.eval,
    #     )

    # Uncomment this to add save replay when evaluation
    # evaluate_env.enable_save_replay(cfg.env.replay_path)

    collector_env.seed(seed)
    # evaluate_env.seed(seed)
    set_pkg_seed(seed)

    policy_cls, model_cls = get_cls(args.policy)
    model = model_cls(**cfg.policy.model)
    policy = policy_cls(cfg.policy, model=model)

    tb_logger = SummaryWriter('./log/{}/'.format(cfg.exp_name))
    learner = BaseLearner(cfg.policy.learn.learner, policy.learn_mode, tb_logger, exp_name=cfg.exp_name)
    collector = SampleSerialCollector(cfg.policy.collect.collector, collector_env, policy.collect_mode, tb_logger, exp_name=cfg.exp_name)
    # evaluator = SerialEvaluator(cfg.policy.eval.evaluator, evaluate_env, policy.eval_mode, tb_logger, exp_name=cfg.exp_name)

    # initiate replay buffer
    if cfg.policy.get('priority', False):
        replay_buffer = AdvancedReplayBuffer(cfg.policy.other.replay_buffer, tb_logger, exp_name=cfg.exp_name)
    else:
        replay_buffer = NaiveReplayBuffer(cfg.policy.other.replay_buffer, tb_logger, exp_name=cfg.exp_name)

    # initiate epislon greedy
    if args.policy == 'dqn':
        eps_cfg = cfg.policy.other.eps
        epsilon_greedy = get_epsilon_greedy_fn(eps_cfg.start, eps_cfg.end, eps_cfg.decay, eps_cfg.type)

    learner.call_hook('before_run')

    # initiate replay buffer and push the first step
    if args.policy != 'ppo':
        if args.policy == 'dqn':
            eps = epsilon_greedy(collector.envstep)
            new_data = collector.collect(n_sample=10000, train_iter=learner.train_iter, policy_kwargs={'eps': eps})
        else:
            new_data = collector.collect(n_sample=10000, train_iter=learner.train_iter)
        replay_buffer.push(new_data, cur_collector_envstep=collector.envstep)

    
    learner.call_hook('after_run')

    collector.close()
    # evaluator.close()
    learner.close()
    if args.policy != 'ppo':
        replay_buffer.close()
  
    print('finish')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='simple-rl train')
    parser.add_argument('-n', '--name', type=str, default='simple-rl', help='experiment name')
    parser.add_argument('-p', '--policy', default='dqn', choices=['dqn', 'ppo', 'td3', 'sac', 'ddpg'], help='RL policy')
    parser.add_argument('-d', '--ding-cfg', default=None, help='DI-engine config path')
    
    args = parser.parse_args()
    main(args)
