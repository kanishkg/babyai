import gym
import torch
from torch import nn
from torch import optim
from torch.nn import functional as F
from torch.utils.data import TensorDataset, DataLoader
from torch import optim
import numpy as np

import copy
import babyai.utils as utils
from babyai.rl import DictList
from babyai.model import ACModel
import multiprocessing
import os
import json
import logging
from torch.autograd import Variable
logger = logging.getLogger(__name__)


class EvalLearner(nn.Module):
    """
    Meta Learner
    """
    def __init__(self, args):
        """
        :param args:
        """
        super(EvalLearner, self).__init__()

        self.update_lr = args.update_lr
        self.meta_lr = args.meta_lr
        self.task_num = args.task_num
        self.args = args

        utils.seed(self.args.seed)


        self.env = gym.make(self.args.env)

        demos_path = utils.get_demos_path(args.demos, args.env, args.demos_origin, valid=False)
        demos_path_valid = utils.get_demos_path(args.demos, args.env, args.demos_origin, valid=True)

        logger.info('loading demos')
        self.train_demos = utils.load_demos(demos_path)
        logger.info('loaded demos')
        # if args.episodes:
        #     if args.episodes > len(self.train_demos):
        #         raise ValueError("there are only {} train demos".format(len(self.train_demos)))
        # self.train_demos = self.train_demos[:args.episodes]

        self.val_demos = utils.load_demos(demos_path_valid)
        # if args.val_episodes > len(self.val_demos):
        #     logger.info('Using all the available {} demos to evaluate valid. accuracy'.format(len(self.val_demos)))
        self.val_demos = self.val_demos[:self.args.val_episodes]

        observation_space = self.env.observation_space
        action_space = self.env.action_space

        print(args.model)
        self.obss_preprocessor = utils.ObssPreprocessor(args.model, observation_space,
                                                        getattr(self.args, 'pretrained_model', None))

        # Define actor-critic model
        self.net = utils.load_model(args.model, raise_not_found=True)
        # if self.net is None:
        #     if getattr(self.args, 'pretrained_model', None):
        #         self.net = utils.load_model(args.pretrained_model, raise_not_found=True)
        #     else:
        # self.net = ACModel(self.obss_preprocessor.obs_space, action_space,
                                       # args.image_dim, args.memory_dim, args.instr_dim,
                                       # not self.args.no_instr, self.args.instr_arch,
                                       # not self.args.no_mem, self.args.arch)
        self.obss_preprocessor.vocab.save()
        # utils.save_model(self.net, args.model)
        self.fast_net = copy.deepcopy(self.net)
        self.net.train()
        self.fast_net.train()

        if torch.cuda.is_available():
            self.net.cuda()
            self.fast_net.cuda()

        self.optimizer = torch.optim.SGD(self.fast_net.parameters(), lr= self.args.update_lr)
        # self.scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size=100, gamma=0.9)

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.meta_optim = optim.Adam(self.net.parameters(), lr=self.meta_lr)


    def starting_indexes(self, num_frames):
        if num_frames % self.args.recurrence == 0:
            return np.arange(0, num_frames, self.args.recurrence)
        else:
            return np.arange(0, num_frames, self.args.recurrence)[:-1]


    def forward_batch(self, batch, task, net = 'fast', is_training = True):
        if net == 'fast':
            acmodel = self.fast_net
        else:
            acmodel = self.net



        batch = utils.demos.induce_grammar(batch, task)

        batch = utils.demos.transform_demos(batch)
        batch.sort(key=len, reverse=True)
        # Constructing flat batch and indices pointing to start of each demonstration
        flat_batch = []
        inds = [0]

        for demo in batch:
            flat_batch += demo
            inds.append(inds[-1] + len(demo))

        flat_batch = np.array(flat_batch)
        inds = inds[:-1]
        num_frames = len(flat_batch)

        mask = np.ones([len(flat_batch)], dtype=np.float64)
        mask[inds] = 0
        mask = torch.tensor(mask, device=self.device, dtype=torch.float).unsqueeze(1)

        # Observations, true action, values and done for each of the stored demostration
        obss, action_true, done = flat_batch[:, 0], flat_batch[:, 1], flat_batch[:, 2]
        action_true = torch.tensor([action for action in action_true], device=self.device, dtype=torch.long)

        # Memory to be stored
        memories = torch.zeros([len(flat_batch), acmodel.memory_size], device=self.device)
        episode_ids = np.zeros(len(flat_batch))
        memory = torch.zeros([len(batch), acmodel.memory_size], device=self.device)

        preprocessed_first_obs = self.obss_preprocessor(obss[inds], device=self.device)
        instr_embedding = acmodel._get_instr_embedding(preprocessed_first_obs.instr)

        # Loop terminates when every observation in the flat_batch has been handled
        while True:
            # taking observations and done located at inds
            obs = obss[inds]
            done_step = done[inds]
            preprocessed_obs = self.obss_preprocessor(obs, device=self.device)
            with torch.no_grad():
                # taking the memory till len(inds), as demos beyond that have already finished
                new_memory = acmodel(
                    preprocessed_obs,
                    memory[:len(inds), :], instr_embedding[:len(inds)])['memory']

            memories[inds, :] = memory[:len(inds), :]
            memory[:len(inds), :] = new_memory
            episode_ids[inds] = range(len(inds))

            # Updating inds, by removing those indices corresponding to which the demonstrations have finished
            inds = inds[:len(inds) - sum(done_step)]
            if len(inds) == 0:
                break

            # Incrementing the remaining indices
            inds = [index + 1 for index in inds]
        # Here, actual backprop upto args.recurrence happens
        final_loss = 0
        final_entropy, final_policy_loss, final_value_loss = 0, 0, 0

        indexes = self.starting_indexes(num_frames)
        memory = memories[indexes]
        accuracy = 0
        total_frames = len(indexes) * self.args.recurrence
        for _ in range(self.args.recurrence):
            obs = obss[indexes]
            preprocessed_obs = self.obss_preprocessor(obs, device=self.device)
            action_step = action_true[indexes]
            mask_step = mask[indexes]
            model_results = acmodel(
                preprocessed_obs, memory * mask_step,
                instr_embedding[episode_ids[indexes]])
            dist = model_results['dist']
            memory = model_results['memory']

            entropy = dist.entropy().mean()
            policy_loss = -dist.log_prob(action_step).mean()
            loss = policy_loss - self.args.entropy_coef * entropy
            action_pred = dist.probs.max(1, keepdim=True)[1]
            accuracy += float((action_pred == action_step.unsqueeze(1)).sum()) / total_frames
            final_loss += loss
            final_entropy += entropy
            final_policy_loss += policy_loss
            indexes += 1

        final_loss /= self.args.recurrence

        # if is_training:
        #     self.optimizer.zero_grad()
        #     final_loss.backward()
        #     self.optimizer.step()

        log = {}
        log["entropy"] = float(final_entropy / self.args.recurrence)
        log["policy_loss"] = float(final_policy_loss / self.args.recurrence)
        log["accuracy"] = float(accuracy)
        return final_loss,log



    def validate(self, demo):
        val_task_num = self.args.task_num

        losses = []  # losses_q[i], i is tasks idx
        logs = []
        val_logs = []
        
        for i in range(99):
            self.fast_net = copy.deepcopy(self.net)
            self.fast_net.train()
            self.fast_net.zero_grad()
              
            # optimize fast net for k isntances of task i
            for k in range(5):
                loss_task, log = self.forward_batch(demo[k*10:10*k+10], 119-i, 'fast')

                self.optimizer.zero_grad()
                loss_task.backward()
                self.optimizer.step()
            # loss_task, log = self.forward_batch(demo, i, 'fast')
            # losses.append(loss_task)
                logs.append(log)
            self.fast_net.eval()
            loss_task, log = self.forward_batch(demo, i, 'fast')
            val_logs.append(log)

        return val_logs



