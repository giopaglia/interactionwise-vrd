import os
import os.path as osp
import sys
from datetime import datetime
import time

import pickle
import yaml
from munch import munchify, unmunchify
from tabulate import tabulate
import warnings

import random
random.seed(0) # only for debugging (i.e TODO remove)
import numpy as np
np.random.seed(0) # only for debugging (i.e TODO remove)
import torch
torch.manual_seed(0) # only for debugging (i.e TODO remove)

import globals, utils

from lib.vrd_models import VRDModel
from lib.dataset import VRDDataset
from lib.datalayer import VRDDataLayer, net_input_to
from lib.evaluator import VRDEvaluator

# Test if code compiles
TEST_DEBUGGING = False # True # False # True # True # False
# Test if a newly-introduced change affects the validity of the code
TEST_EVAL_VALIDITY = False # False # True # False #  True # True
TEST_TRAIN_VALIDITY = False # False # True # False #  True # True
# Try overfitting to a single element
TEST_OVERFIT = False #True # False # True

if utils.device == torch.device("cpu"):
  DEBUGGING = True


class vrd_trainer():

  def __init__(self, session_name, args = {}, profile = None, checkpoint = False):

    # Load arguments cascade from profiles
    def_args = utils.load_cfg_profile("default")
    if profile is not None:
      profile = utils.listify(profile)
      for p in profile:
        def_args = utils.dict_patch(utils.load_cfg_profile(p), def_args)
    args = utils.dict_patch(args, def_args)

    #print("Arguments:\n", yaml.dump(args, default_flow_style=False))

    self.session_name = session_name
    self.args         = args
    self.checkpoint   = checkpoint

    self.state        = {"epoch" : 0}

    # Load checkpoint, if any
    if isinstance(self.checkpoint, str):

      checkpoint_path = osp.join(globals.models_dir, self.checkpoint)
      print("Loading checkpoint... ({})".format(checkpoint_path))

      if not osp.isfile(checkpoint_path):
        raise Exception("Checkpoint not found: {}".format(checkpoint_path))

      checkpoint = utils.load_checkpoint(checkpoint_path)
      #print(checkpoint.keys())

      # Session name
      utils.patch_key(checkpoint, "session", "session_name") # (patching)
      if "session_name" in checkpoint:
        self.session_name = checkpoint["session_name"]

      # Arguments
      if "args" in checkpoint:
        self.args = checkpoint["args"]

      # State
      # Epoch
      utils.patch_key(checkpoint, "epoch", ["state", "epoch"]) # (patching)
      # Model state dictionary
      utils.patch_key(checkpoint, "state_dict", ["state", "model_state_dict"]) # (patching)
      utils.patch_key(checkpoint["state"]["model_state_dict"], "fc_so_emb.fc.weight", "fc_semantic.fc.weight") # (patching)
      utils.patch_key(checkpoint["state"]["model_state_dict"], "fc_so_emb.fc.bias",   "fc_semantic.fc.bias") # (patching)
      # TODO: is checkpoint["model_state_dict"]["emb.weight"] different from the weights used for initialization...?
      # Optimizer state dictionary
      utils.patch_key(checkpoint, "optimizer", ["state", "optimizer_state_dict"]) # (patching)
      self.state = checkpoint["state"]

    print("Arguments:\n", yaml.dump(self.args, default_flow_style=False))
    self.args = munchify(self.args)

    # TODO: change split to avoid overfitting on this split! (https://towardsdatascience.com/understanding-pytorch-with-an-example-a-step-by-step-tutorial-81fc5f8c4e8e)
    #  train_dataset, val_dataset = random_split(dataset, [80, 20])

    # Data
    print("Initializing data: ", self.args.data)
    self.dataset = VRDDataset(**self.args.data)

    # Model
    self.args.model.n_obj  = self.dataset.n_obj
    self.args.model.n_pred = self.dataset.n_pred
    if self.args.model.use_pred_sem:
      self.args.model.pred_emb = np.array(self.dataset.readJSON("predicates-emb.json"))
    # ... if self.args.model.use_pred_sem:
    #   self.args.pP_prior = self.dataset.getDistribution("pP")
    print("Initializing VRD Model: ", self.args.model)
    self.model = VRDModel(self.args.model).to(utils.device)
    if "model_state_dict" in self.state:
      print("Loading state_dict")
      self.model.load_state_dict(self.state["model_state_dict"])
    else:
      print("Random initialization...")
      # Random initialization
      utils.weights_normal_init(self.model, dev=0.01)
      # Load existing embeddings
      if self.model.args.use_sem != False:
        try:
          # obj_emb = torch.from_numpy(self.dataset.readPKL("params_emb.pkl"))
          obj_emb = torch.from_numpy(np.array(self.dataset.readJSON("objects-emb.json")))
        except FileNotFoundError:
          print("Warning! Initialization weights for emb.weight layer not found! Weights are initialized randomly")
          input()
          # set_trainability(self.model.emb, requires_grad=True)
        self.model.state_dict()["emb.weight"].copy_(obj_emb)

    # DataLoader
    # TODO? VRDDataLayer has to know what to yield (DRS -> img_blob, obj_boxes, u_boxes, idx_s, idx_o, spatial_features, obj_classes)
    self.datalayer = VRDDataLayer(self.dataset, "train", use_preload = self.args.training.use_preload, cols = self.model.input_cols)
    self.dataloader = torch.utils.data.DataLoader(
      dataset = self.datalayer,
      batch_size = 1, # self.args.training.batch_size,
      # sampler= Random ...,
      num_workers = 4, # num_workers=self.num_workers
      pin_memory = True,
      shuffle = self.args.training.use_shuffle,
    )

    # Evaluation
    print("Initializing evaluator...")
    self.eval = VRDEvaluator(self.dataset, self.args.eval, input_cols = self.model.input_cols)

    # Training
    print("Initializing training...")
    self.optimizer = self.model.OriginalAdamOptimizer(**self.args.opt)

    self.criterions = {}
    if "mlab" in self.args.training.loss:
      self.criterions["mlab"] = torch.nn.MultiLabelMarginLoss(reduction="sum").to(device=utils.device)
    if "mse" in self.args.training.loss:
      self.criterions["mse"] = torch.nn.MSELoss(reduction="sum").to(device=utils.device)
    if "cross-entropy" in self.args.training.loss:
      self.criterions["cross-entropy"] = torch.nn.CrossEntropyLoss(reduction="sum").to(device=utils.device)
    if not len(self.criterions):
      raise ValueError("Unknown loss specified: '{}'".format(self.args.training.loss))

    if "optimizer_state_dict" in self.state:
      print("Loading optimizer state_dict...")
      self.optimizer.load_state_dict(self.state["optimizer_state_dict"])
    elif isinstance(self.checkpoint, str):
      print("Warning! Optimizer state_dict not found!")

    self.args.eval.rec = sorted(self.args.eval.rec, reverse=True)

  # Perform the complete training process
  def train(self):
    print("train()")

    # Prepare result table
    res_headers = ["Epoch"]
    if self.args.eval.test_pre: res_headers += self.gt_headers(self.args.eval.test_pre, "Pre") + ["Avg"]
    if self.args.eval.test_rel: res_headers += self.gt_headers(self.args.eval.test_rel, "Rel") + ["Avg"]

    res = []

    if self.args.training.test_first:
      self.do_test(res, res_headers, self.state["epoch"])

    end_epoch = self.state["epoch"] + self.args.training.num_epochs
    while self.state["epoch"] < end_epoch:

      print("Epoch {}/{}".format((self.state["epoch"]+1), end_epoch))


      # TODO check if this works (Note that you'd have to make it work cross-sessions as well)
      # if (self.state["epoch"] % (self.args.training.lr_decay_step + 1)) == 0:
      #   print("*adjust_learning_rate*")
      #   utils.adjust_learning_rate(self.optimizer, self.args.training.lr_decay_gamma)
      # TODO do it with the scheduler, see if it's the same: https://pytorch.org/docs/stable/optim.html#how-to-adjust-learning-rate
      # exp_lr_scheduler = lr_scheduler.StepLR(self.optimizer, step_size=5, gamma=0.1)

      # TODO: after the third epoch, we divide learning rate by 10
      # the authors mention doing this in their paper, but we couldn't find it in the actual code
      if self.state["epoch"] == 3:
        print("Dividing the learning rate by 10 at epoch {}!".format(self.state["epoch"]))
        for i in range(len(self.optimizer.param_groups)):
          self.optimizer.param_groups[i]["lr"] /= 10

      # Train
      self.__train_epoch()

      # Save test results, save checkpoint
      is_last_epoch = ((end_epoch-self.state["epoch"]) <= 1)
      do_save_checkpoint = (is_last_epoch and float(self.args.training.checkpoint_freq) != 0.0) or utils.smart_frequency_check(self.state["epoch"], end_epoch, self.args.training.checkpoint_freq, last = True)
      do_test = is_last_epoch or do_save_checkpoint or utils.smart_frequency_check(self.state["epoch"], end_epoch, self.args.training.test_freq, last = True)

      if do_test:
        self.do_test(res, res_headers)

      if do_save_checkpoint:

        save_dir = osp.join(globals.models_dir, self.session_name)
        if not osp.exists(save_dir):
          os.mkdir(save_dir)

        # TODO: the loss should be a result: self.result.loss (which is ignored at loading,only used when saving checkpoint)...
        self.state["model_state_dict"]     = self.model.state_dict()
        self.state["optimizer_state_dict"] = self.optimizer.state_dict()

        utils.save_checkpoint({
          "session_name"  : self.session_name,
          "args"          : unmunchify(self.args),
          "state"         : self.state,
          "result"        : dict(zip(res_headers, res[-1])),
        }, osp.join(save_dir, "checkpoint_epoch_{}.pth.tar".format(self.state["epoch"])))

      self.state["epoch"] += 1


  def do_test(self, res, res_headers, force_epoch = None):
    epoch = self.state["epoch"]+1
    if force_epoch is not None: epoch = force_epoch
    if self.args.eval.test_pre:
      recalls, dtime = self.test_pre()
      res_row += recalls + (sum(recalls)/len(recalls),)
    if self.args.eval.test_rel:
      recalls, dtime = self.test_rel()
      res_row += recalls + (sum(recalls)/len(recalls),)
    res.append(res_row)
    with open(osp.join(globals.models_dir, "{}.txt".format(self.session_name)), 'w') as f:
      f.write(tabulate(res, res_headers))

  def __train_epoch(self):
    self.model.train()

    time1 = time.time()
    # TODO check if LeveledAverageMeter works
    losses = utils.LeveledAverageMeter(2)

    # Iterate over the dataset
    n_iter = len(self.dataloader)

    for i_iter,(net_input, gt_soP_prior, gt_pred_sem, mlab_target) in enumerate(self.dataloader):

      if utils.smart_frequency_check(i_iter, n_iter, self.args.training.print_freq):
        print("\t{:4d}/{:<4d}: ".format(i_iter, n_iter), end="")

      net_input    = net_input_to(net_input, utils.device)
      gt_soP_prior = gt_soP_prior.to(utils.device)
      gt_pred_sem  = torch.as_tensor(gt_pred_sem,    dtype=torch.long,     device = utils.device)
      mlab_target  = torch.as_tensor(mlab_target,    dtype=torch.long,     device = utils.device)

      batch_size = mlab_target.size()[0]

      # Forward pass & Backpropagation step
      self.optimizer.zero_grad()
      model_output = self.model(*net_input)


      _, rel_scores, pred_sem = model_output

      loss = 0
      if "mlab" in self.args.training.loss:
        # DSR:
        # TODO: fix this weird-shaped mlab_target in datalayer and remove this view thingy
        if not "mlab_no_prior" in self.args.training.loss:
          # Note that maybe introducing no_predicate may be better:
          #  After all, there may not be a relationship between two objects...
          #  And this would avoid dirtying up the predictions?
          # TODO: change some constant to the gt_soP_prior before factoring it into the loss
          gt_soP_prior = -0.5 * ( gt_soP_prior + (1.0 / self.dataset.n_pred))
          loss += self.criterions["mlab"]((gt_soP_prior + rel_scores).view(batch_size, -1), mlab_target)
        else:
          loss += self.criterions["mlab"]((rel_scores).view(batch_size, -1), mlab_target)
      if "mse" in self.args.training.loss:
        # TODO use the weighted embeddings of gt_soP_prior ?
        loss += self.criterions["mse"](pred_sem, gt_pred_sem)

      loss.backward()
      self.optimizer.step()

      # Track loss
      losses.update(loss.item())
      if utils.smart_frequency_check(i_iter, n_iter, self.args.training.print_freq, last=True):
        print("LOSS: {: 6.3f}\n".format(losses.avg(0)), end="")
        losses.reset(0)

    self.state["loss"] = losses.avg(1)
    time2 = time.time()

    print("TRAIN Loss: {: 6.3f} Time: {}".format(self.state["loss"], utils.time_diff_str(time1, time2)))

    """
      # the gt_soP_prior here is a subset of the 100*70*70 dimensional so_prior array, which contains the predicate prob distribution for all object pairs
      # the gt_soP_prior here contains the predicate probability distribution of only the object pairs in this annotation
      # Also, it might be helpful to keep in mind that this data layer currently works for a single annotation at a time - no batching is implemented!
    """

  def test_pre(self):
    recalls, dtime = self.eval.test_pre(self.model, self.args.eval.rec)
    print("PRED TEST:")
    print(self.get_format_str(self.gt_headers(self.args.eval.test_pre)).format(*recalls))
    print("TEST Time: {}".format(utils.time_diff_str(dtime)))
    return recalls, dtime

  def test_rel(self):
    recalls, (pos_num, loc_num, gt_num), dtime = self.eval.test_rel(self.model, self.args.eval.rec)
    print("REL TEST:")
    print(self.get_format_str(self.gt_headers(self.args.eval.test_rel)).format(*recalls))
    # print("OBJ TEST: POS: {: 6.3f}, LOC: {: 6.3f}, GT: {: 6.3f}, Precision: {: 6.3f}, Recall: {: 6.3f}".format(pos_num, loc_num, gt_num, np.float64(pos_num)/(pos_num+loc_num), np.float64(pos_num)/gt_num))
    print("TEST Time: {}".format(utils.time_diff_str(dtime)))
    return recalls, dtime

  def get_format_str(self, gt_headers):
    return "".join(["\t{}: {{: 6.3f}}".format(x) if i % 2 == 0 else "\t{}: {{: 6.3f}}\n".format(x) for i,x in enumerate(gt_headers)])

  def gt_headers(self, test_type, prefix=""):
    def metric_name(x):
      if isinstance(x, float): return "{}x".format(x)
      elif isinstance(x, int): return str(x)
    if prefix == "":        fst_col_prefix = ""
    elif test_type != True: fst_col_prefix = "{} {} ".format(prefix, test_type)
    else:                   fst_col_prefix = "{} ".format(prefix)
    headers = []
    for i,x in enumerate(self.args.eval.rec):
      if i == 0: name = fst_col_prefix
      else:      name = ""
      name += "R@" + metric_name(x)
      headers.append(name)
      headers.append("ZS")
    return headers

if __name__ == "__main__":

  test_type = True # 0.2

  # DEBUGGING: Test if code compiles
  if TEST_DEBUGGING:
    print("########################### TEST_DEBUGGING ###########################")
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    trainer = vrd_trainer("test", {"training" : {"num_epochs" : 1, "test_first" : True},
        "eval" : {"test_pre" : test_type,  "test_rel" : test_type},
        "data" : {"justafew" : True}}) #, checkpoint="epoch_4_checkpoint.pth.tar")
    trainer.train()


  # TEST_TRAIN_VALIDITY or TEST_EVAL_VALIDITY:
  #  Test if a newly-introduced change affects the validity of the evaluation (or evaluation+training)
  if TEST_TRAIN_VALIDITY or TEST_EVAL_VALIDITY:
    print("########################### TEST_VALIDITY ###########################")
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    dataset_name = "vrd/dsr"
    if TEST_EVAL_VALIDITY:
      trainer = vrd_trainer("original-checkpoint", {"training" : {"num_epochs" : 1, "test_first" : True}, "eval" : {"test_pre" : test_type,  "test_rel" : test_type},  "data" : {"name" : dataset_name}, "model" : {"use_spat" : 0}}, checkpoint="epoch_4_checkpoint.pth.tar")
      trainer.train()
    if TEST_TRAIN_VALIDITY:
      trainer = vrd_trainer("original", {"training" : {"num_epochs" : 5, "test_first" : True}, "eval" : {"test_pre" : test_type,  "test_rel" : test_type},  "data" : {"name" : dataset_name}})
      trainer.train()

  # TEST_OVERFIT: Try overfitting the network to a single batch
  if TEST_OVERFIT:
        print("########################### TEST_OVERFIT ###########################")
        justafew = 3
        torch.backends.cudnn.deterministic = False
        torch.backends.cudnn.benchmark = True
        random.seed(datetime.now())
        np.random.seed(datetime.now())
        torch.manual_seed(datetime.now())
        args = {"training" : {"num_epochs" : 6}, "eval" : {"test_pre" : test_type,  "test_rel" : test_type, "justafew" : justafew},  "data" : {"justafew" : justafew}}
        #args = {"model" : {"use_pred_sem" : ...}, "num_epochs" : 5, "opt": {"lr": lr, "weight_decay" : weight_decay, "lr_fus_ratio" : lr_rel_fus_ratio, "lr_rel_ratio" : lr_rel_fus_ratio}}}
        trainer = vrd_trainer("test-overfit", args = args, profile = ["vg", "pred_sem"])
        trainer.train()

      # Test VG
      # trainer = vrd_trainer("original-vg", {"training" : {"test_first" : True, "num_epochs" : 5}, "eval" : {"test_pre" : False, "test_rel" : test_type}}, profile = "vg")
      #trainer = vrd_trainer("original-vg", {"training" : {"test_first" : True, "num_epochs" : 5}, "eval" : {"test_pre" : test_type}}, profile = "vg")
      #trainer.train()

  #trainer = vrd_trainer("test-no-features",  {"data" : {"justafew" : 3}, "eval" : {"justafew" : 3, "test_rel":False}, "training" : {"num_epochs" : 4, "test_first" : True, "loss" : "mlab_no_prior"}, "model" : {"use_vis" : False, "use_so" : False, "use_sem" : 0, "use_spat" : 0}})
  trainer = vrd_trainer("test-no-features",  {"eval" : {"test_rel":False}, "training" : {"num_epochs" : 4, "test_first" : True, "loss" : "mlab_no_prior"}, "model" : {"use_vis" : False, "use_so" : False, "use_sem" : 0, "use_spat" : 0}})
  trainer.train()
  sys.exit(0)

  #trainer = vrd_trainer("test-original", {"training" : {"num_epochs" : 5}, "eval" : {"test_pre" : test_type,  "test_rel" : test_type},  "data" : {"name" : "vrd"}})
  #trainer.train()

  trainer.train()
  sys.exit(0)

  #trainer = vrd_trainer("test-original", {"training" : {"num_epochs" : 5}, "eval" : {"test_pre" : test_type,  "test_rel" : test_type},  "data" : {"name" : "vrd"}})
  #trainer.train()

  #trainer = vrd_trainer("test-original-no_prior", {"training" : {"num_epochs" : 5}, "eval" : {"test_pre" : test_type,  "test_rel" : test_type},  "data" : {"name" : "vrd"}})
  #trainer.train()

  #trainer = vrd_trainer("test-no_prior-only_vis",  {"training" : {"num_epochs" : 4, "loss" : "mlab_no_prior"}, "model" : {"use_sem" : 0, "use_spat" : 0}})
  #trainer.train()
  #trainer = vrd_trainer("test-no_prior-only_sem",  {"training" : {"num_epochs" : 4, "loss" : "mlab_no_prior"}, "model" : {"use_vis" : False, "use_so" : False, "use_spat" : 0}})
  #trainer.train()
  #trainer = vrd_trainer("test-no_prior-only_spat", {"training" : {"num_epochs" : 4, "loss" : "mlab_no_prior"}, "model" : {"use_vis" : False, "use_so" : False, "use_sem" : 0}})
  #trainer.train()

  # Scan (rotating parameters)
  for lr in [0.0001]: # , 0.00001, 0.000001]: # [0.001, 0.0001, 0.00001, 0.000001]:
    for weight_decay in [0.0001]: # , 0.00005]:
      for lr_fus_ratio in [10]:
        for lr_rel_ratio in [10]:
          for pred_sem_mode_1 in [-1, 16, 17, 18, 19, 24, 25, 26, 27]: # 16+0 [1,8+1,16+0,16+4]: # , 16+0,16+1,16+2, 16+8+0, 16+8+4+0, 16+16+0]:
            for loss in ["mlab"]: # , "mlab_mse", "mse"]:
              for dataset in ["vrd"]: # , "vg"]:
                if pred_sem_mode_1 == -1 and "mse" in loss:
                  continue
                pred_sem_mode = pred_sem_mode_1+1
                session_id = "scan-v10-{}-{}-{}-{}-{}-{}-{}".format(lr, weight_decay, lr_fus_ratio, lr_rel_ratio, pred_sem_mode, dataset, loss)
                profile = ["pred_sem"]
                training = {"num_epochs" : 4, "test_freq" : [1,2,3]}
                if dataset == "vg":
                  profile.append("vg")
                  training = {"num_epochs" : 2, "test_freq" : [1,2]}
                test_type = True # 0.5

                trainer = vrd_trainer(session_id, {
                  # "data" : {"justafew" : True}, "training" : {"num_epochs" : 2, "test_freq" : 0},
                  "training" : training,
                  "model" : {"use_pred_sem" : pred_sem_mode},
                  "eval" : {"rec" : [50, 30], "test_pre" : test_type}, # "test_rel" : test_type},
                  "opt": {
                    "lr": lr,
                    "weight_decay" : weight_decay,
                    "lr_fus_ratio" : lr_fus_ratio,
                    "lr_rel_ratio" : lr_rel_ratio,
                    }
                  }, profile = profile)
                trainer.train()


  sys.exit(0)
