import numpy as np
from multiprocessing import Manager

from iroko_monitor import StatsCollector
from iroko_monitor import FlowCollector
from iroko_reward import RewardFunction


REWARD_MODEL = ["bw", "queue"]
###########################################


class StateManager():
    DELTA_KEYS = ["delta_q_abs"]
    STATE_KEYS = ["queues"]
    COLLECT_FLOWS = True

    def __init__(self, topo_conf, config, reward_fun=REWARD_MODEL):
        self.conf = config
        self.ports = topo_conf.get_sw_ports()
        self.num_ports = len(self.ports)
        self.topo_conf = topo_conf
        self._set_feature_length()
        self._set_reward(reward_fun, topo_conf)
        self.spawn_collectors(topo_conf.host_ips)
        self.time_step_reward = []
        self.queues_per_port = {k: [] for k in self.ports}
        self.action_per_port = {k: []
                                for k in topo_conf.host_ctrl_map}
        self.bws_per_port = {}
        self.bws_per_port["tx"] = {k: []
                                   for k in topo_conf.host_ctrl_map}
        self.bws_per_port["rx"] = {k: []
                                   for k in topo_conf.host_ctrl_map}

    def terminate(self):
        self.save()
        self._terminate_collectors()

    def reset(self):
        self.save()
        # self.time_step_reward = []
        # self.queues_per_port = {k: [] for k in self.ports}
        # self.action_per_port = {k: []
        #                         for k in self.topo_conf.host_ctrl_map}
        # self.bws_per_port = {}
        # self.bws_per_port["tx"] = {k: []
        #                            for k in self.topo_conf.host_ctrl_map}
        # self.bws_per_port["rx"] = {k: []
        #                            for k in self.topo_conf.host_ctrl_map}

    def _set_feature_length(self):
        self.num_features = len(self.DELTA_KEYS)
        self.num_features += len(self.STATE_KEYS)
        if (self.COLLECT_FLOWS):
            self.num_features += len(self.topo_conf.host_ips) * 2

    def _set_reward(self, reward_fun, topo_conf):
        self.dopamin = RewardFunction(topo_conf.host_ctrl_map,
                                      self.ports,
                                      reward_fun, topo_conf.MAX_QUEUE,
                                      topo_conf.MAX_CAPACITY)

    def spawn_collectors(self, host_ips):
        manager = Manager()
        self.stats = manager.dict()
        self.stats_proc = StatsCollector(self.ports, self.stats)
        # self.stats_proc.daemon = True
        self.stats_proc.start()
        # Launch an asynchronous flow collector
        self.src_flows = manager.dict()
        self.dst_flows = manager.dict()
        self.flows_proc = FlowCollector(
            self.ports, host_ips, self.src_flows, self.dst_flows)
        # self.flows_proc.daemon = True
        self.flows_proc.start()
        # initialize the stats matrix
        self.prev_stats = self.stats.copy()

    def _terminate_collectors(self):
        if (self.stats_proc is not None):
            self.stats_proc.terminate()
        if (self.flows_proc is not None):
            self.flows_proc.terminate()

    def _compute_delta(self, stats_prev, stats_now):
        deltas = {}
        for iface in stats_prev.keys():
            bws_rx_prev = stats_prev[iface]["bws_rx"]
            bws_tx_prev = stats_prev[iface]["bws_tx"]
            drops_prev = stats_prev[iface]["drops"]
            overlimits_prev = stats_prev[iface]["overlimits"]
            queues_prev = stats_prev[iface]["queues"]

            bws_rx_now = stats_now[iface]["bws_rx"]
            bws_tx_now = stats_now[iface]["bws_tx"]
            drops_now = stats_now[iface]["drops"]
            overlimits_now = stats_now[iface]["overlimits"]
            queues_now = stats_now[iface]["queues"]

            deltas[iface] = {}
            if bws_rx_prev <= bws_rx_now:
                deltas[iface]["delta_rx"] = 1
            else:
                deltas[iface]["delta_rx"] = 0

            if bws_tx_prev <= bws_tx_now:
                deltas[iface]["delta_tx"] = 1
            else:
                deltas[iface]["delta_tx"] = 0

            if drops_prev < drops_now:
                deltas[iface]["delta_d"] = 0
            else:
                deltas[iface]["delta_d"] = 1

            if overlimits_prev < overlimits_now:
                deltas[iface]["delta_ov"] = 0
            else:
                deltas[iface]["delta_ov"] = 1

            if queues_prev < queues_now:
                deltas[iface]["delta_q"] = 1
            elif queues_prev > queues_now:
                deltas[iface]["delta_q"] = -1
            else:
                deltas["delta_q"] = 0
            deltas[iface]["delta_q_abs"] = queues_now - queues_prev
            deltas[iface]["delta_rx_abs"] = bws_rx_now - bws_rx_prev
            deltas[iface]["delta_tx_abs"] = bws_tx_now - bws_tx_prev
        return deltas

    def collect(self):

        obs = np.zeros((self.num_ports, self.num_features))

        # retrieve the current deltas before updating total values
        delta_vector = self._compute_delta(self.prev_stats, self.stats)
        self.prev_stats = self.stats.copy()
        # Create the data matrix for the agent based on the collected stats
        for i, iface in enumerate(self.ports):
            state = []
            deltas = delta_vector[iface]
            for key in self.DELTA_KEYS:
                state.append(deltas[key])
            for key in self.STATE_KEYS:
                state.append(self.stats[iface][key])
            if self.COLLECT_FLOWS:
                state += self.src_flows[iface]
                state += self.dst_flows[iface]
            # print("State %s: %s " % (iface, state))
            obs[i] = np.array(state)

            if iface in self.queues_per_port:
                self.queues_per_port[iface].append(self.stats[iface]["queues"])

            if iface in self.topo_conf.host_ctrl_map:
                self.bws_per_port["rx"][iface].append(
                    self.stats[iface]["bws_rx"])
                self.bws_per_port["tx"][iface].append(
                    self.stats[iface]["bws_tx"])
        return obs

    def compute_reward(self, curr_action):
        # Compute the reward
        reward = self.dopamin.get_reward(self.stats, curr_action)
        self.time_step_reward.append(reward)
        if (len(self.time_step_reward) % 1000) == 0:
            self.save()
        for k in curr_action.keys():
            self.action_per_port[k].append(curr_action[k])
        return reward

    def save(self):
        print ("Saving statistics...")
        data_dir = self.conf["output_dir"]
        agent = self.conf["agent"]
        # define file names
        reward_file = "%s/reward_per_step_%s" % (data_dir, agent)
        action_file = "%s/action_per_step_by_port_%s" % (data_dir, agent)
        queue_file = "%s/queues_per_step_by_port_%s" % (data_dir, agent)
        bw_file = "%s/bandwidths_per_step_by_port_%s" % (data_dir, agent)

        np.save(reward_file, self.time_step_reward)
        np.save(action_file, self.action_per_port)
        np.save(queue_file, self.queues_per_port)
        np.save(bw_file, self.bws_per_port)