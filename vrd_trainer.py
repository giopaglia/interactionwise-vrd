import os
import os.path as osp
import sys
from datetime import datetime
import time

import pickle
from munch import munchify
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
import lib.datalayers
from lib.datalayers import VRDDataLayer
from lib.evaluator import VRDEvaluator

# Test if code compiles
TEST_DEBUGGING = False # False # True # False # True # True # False
# Test if a newly-introduced change affects the validity of the code
TEST_VALIDITY = False # True # False # True
# Try overfitting to a single element
TEST_OVERFIT = False #True # False # True

if utils.device == torch.device("cpu"):
  DEBUGGING = True


class vrd_trainer():

  def __init__(self, session_name, args = {}, profile = None, checkpoint = False):

    def_args = utils.cfg_from_file("cfgs/default.yml")
    if profile is not None:
      def_args = utils.dict_patch(utils.cfg_from_file(profile), def_args)
    args = utils.dict_patch(args, def_args)

    print("Arguments:")
    if checkpoint:
      print("Checkpoint: {}", checkpoint)
    else:
      print("No Checkpoint")
    print("args:", yaml.dump(args, default_flow_style=False))


    self.session_name = session_name

    self.checkpoint = checkpoint
    self.args       = args
    self.state      = {"epoch" : 0}



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

    # TODO: idea, don't use data_args.name but data.name?
    self.data_args    = munchify(self.args["data"])
    self.model_args   = munchify(self.args["model"])
    self.eval_args    = munchify(self.args["eval"])
    self.training     = munchify(self.args["training"])

    # TODO: change split to avoid overfitting on this split! (https://towardsdatascience.com/understanding-pytorch-with-an-example-a-step-by-step-tutorial-81fc5f8c4e8e)
    #  train_dataset, val_dataset = random_split(dataset, [80, 20])

    # Data
    print("Initializing data: ", self.data_args)
    # TODO: VRDDataLayer has to know what to yield (DRS -> img_blob, obj_boxes, u_boxes, idx_s, idx_o, spatial_features, obj_classes)
    self.datalayer = VRDDataLayer(self.data_args, "train", use_preload = self.training.use_preload)
    self.dataloader = torch.utils.data.DataLoader(
      dataset = self.datalayer,
      batch_size = 1, # self.training.batch_size,
      # sampler= Random ...,
      num_workers = 2, # num_workers=self.num_workers
      shuffle = self.training.use_shuffle,
    )

    # Model
    self.model_args.n_obj  = self.datalayer.n_obj
    self.model_args.n_pred = self.datalayer.n_pred
    if self.model_args.use_pred_sem != False:
      self.model_args.pred_emb = np.array(self.datalayer.dataset.readJSON("predicates-emb.json"))
    print("Initializing VRD Model: ", self.model_args)
    self.model = VRDModel(self.model_args).to(utils.device)
    if "model_state_dict" in self.state:
      # TODO: Make sure that this doesn't need the random initialization first
      print("Loading state_dict")
      self.model.load_state_dict(self.state["model_state_dict"])
    else:
      print("Random initialization")
      # Random initialization
      utils.weights_normal_init(self.model, dev=0.01)
      # Load VGG layers
      self.model.load_pretrained_conv(osp.join(globals.data_dir, "VGG_imagenet.npy"), fix_layers=True)
      # Load existing embeddings
      try:
        with open(osp.join(self.datalayer.dataset.metadata_dir, "params_emb.pkl"), 'rb') as f:
          self.model.state_dict()["emb.weight"].copy_(torch.from_numpy(pickle.load(f, encoding="latin1")))
      except FileNotFoundError:
        warnings.warn("Initialization weights for emb.weight layer not found!", UserWarning)
    # Evaluation
    print("Initializing evaluator: ", self.eval_args)
    self.eval = VRDEvaluator(self.data_args, self.eval_args)

    # Training
    print("Initializing training: ", self.training)
    self.optimizer = self.model.OriginalAdamOptimizer(**self.training.opt)

    if self.training.loss == "mlab":
      self.criterion = torch.nn.MultiLabelMarginLoss(reduction="sum").to(device=utils.device)
    elif self.training.loss == "cross-entropy":
      self.criterion = torch.nn.CrossEntropyLoss(reduction="sum").to(device=utils.device)
    elif self.training.loss == "mse":
      self.criterion = torch.nn.MSELoss(reduction="sum").to(device=utils.device)
    else:
      raise ValueError("Unknown loss specified: '{}'".format(self.training.loss))

    if "optimizer_state_dict" in self.state:
      self.optimizer.load_state_dict(self.state["optimizer_state_dict"])

  # Perform the complete training process
  def train(self):
    print("train()")

    save_dir = osp.join(globals.models_dir, self.session_name)
    if not osp.exists(save_dir):
      os.mkdir(save_dir)
    save_file = osp.join(globals.models_dir, "{}.txt".format(self.session_name))

    # Prepare result table
    res_headers = ["Epoch"]
    self.eval_args.rec = sorted(self.eval_args.rec, reverse=True)
    if self.eval_args.test_pre: res_headers += self.gt_headers("Pre", self.eval_args.test_pre)
    if self.eval_args.test_rel: res_headers += self.gt_headers("Rel", self.eval_args.test_rel)

    res = []

    end_epoch = self.state["epoch"] + self.training.num_epochs
    while self.state["epoch"] < end_epoch:

      print("Epoch {}/{}".format((self.state["epoch"]+1), end_epoch))


      # TODO check if this works (Note that you'd have to make it work cross-sessions as well)
      # if (self.state["epoch"] % (self.training.lr_decay_step + 1)) == 0:
      #   print("*adjust_learning_rate*")
      #   utils.adjust_learning_rate(self.optimizer, self.training.lr_decay_gamma)
      # TODO do it with the scheduler, see if it's the same: https://pytorch.org/docs/stable/optim.html#how-to-adjust-learning-rate
      # exp_lr_scheduler = lr_scheduler.StepLR(self.optimizer, step_size=5, gamma=0.1)

      # TODO: after the third epoch, we divide learning rate by 10
      # the authors mention doing this in their paper, but we couldn't find it in the actual code
      if self.state["epoch"] != 0 and (self.state["epoch"] % 3) == 0:
        print("Dividing the learning rate by 10 at epoch {}!".format(self.state["epoch"]))
        for i in range(len(self.optimizer.param_groups)):
          self.optimizer.param_groups[i]["lr"] /= 10

      self.__train_epoch()

      # Test results
      if True or utils.smart_frequency_check(self.state["epoch"],
              self.training.num_epochs,
              self.training.checkpoint_freq) or self.state["epoch"] % 2:
        res_row = [self.state["epoch"]]
        if self.eval_args.test_pre:
          recalls, dtime = self.test_pre()
          res_row += recalls
        if self.eval_args.test_rel:
          recalls, dtime = self.test_rel()
          res_row += recalls
        res.append(res_row)

      with open(save_file, 'w') as f:
        f.write(tabulate(res, res_headers))

      # Save checkpoint
      if utils.smart_frequency_check(self.state["epoch"], self.training.num_epochs, self.training.checkpoint_freq):

        # TODO: the loss should be a result: self.result.loss (which is ignored at loading,only used when saving checkpoint)...
        self.state["model_state_dict"]     = self.model.state_dict()
        self.state["optimizer_state_dict"] = self.optimizer.state_dict()

        utils.save_checkpoint({
          "session_name"  : self.session_name,
          "args"          : self.args,
          "state"         : self.state,
          "result"        : dict(zip(res_headers, res_row)),
        }, osp.join(save_dir, "checkpoint_epoch_{}.pth.tar".format(self.state["epoch"])))

      #self.__train_epoch()

      self.state["epoch"] += 1

  def __train_epoch(self):
    self.model.train()

    time1 = time.time()
    # TODO check if LeveledAverageMeter works
    losses = utils.LeveledAverageMeter(2)

    # Iterate over the dataset
    n_iter = len(self.dataloader)

    # for iter in range(n_iter):
    for i_iter,(net_input, gt_soP_prior, gt_pred_sem, mlab_target) in enumerate(self.dataloader):

      # print("{}/{}".format(i_iter, n_iter))

      # print(type(net_input))
      # print(type(gt_soP_prior))
      # print(type(mlab_target))

      net_input = lib.datalayers.net_input_to(net_input, utils.device)
      gt_pred_sem      = torch.as_tensor(gt_pred_sem,    dtype=torch.long,     device = utils.device)
      mlab_target      = torch.as_tensor(mlab_target,    dtype=torch.long,     device = utils.device)

      batch_size = mlab_target.size()[0]

      # Forward pass & Backpropagation step
      self.optimizer.zero_grad()
      model_output = self.model(*net_input)

      # Preprocessing the gt_soP_prior before factoring it into the loss
      gt_soP_prior = gt_soP_prior.to(utils.device)
      # Note that maybe introducing no_predicate may be better:
      #  After all, there may not be a relationship between two objects...
      #  And this would avoid dirtying up the predictions?
      gt_soP_prior = -0.5 * ( gt_soP_prior + (1.0 / self.datalayer.n_pred))

      # DSR:
      # TODO: fix this weird-shaped mlab_target in datalayers and remove this view thingy
      if self.training.loss == "mlab":
        _, rel_scores = model_output
        loss = self.criterion((gt_soP_prior + rel_scores).view(batch_size, -1), mlab_target)
        # loss = self.criterion((rel_scores).view(batch_size, -1), mlab_target)
      elif self.training.loss == "mse":
        _, pred_sem = model_output
        # TODO use the weighted embeddings of gt_soP_prior ?
        loss = self.criterion(pred_sem, gt_pred_sem)

      loss.backward()
      self.optimizer.step()

      # Track loss
      losses.update(loss.item())

      if utils.smart_frequency_check(i_iter, n_iter, self.training.print_freq):
          print("\t{:4d}/{:<4d}: LOSS: {: 6.3f}\r".format(i_iter, n_iter, losses.avg(0)), end="")
          losses.reset(0)

    self.state["loss"] = losses.avg(1)
    time2 = time.time()

    print("TRAIN Loss: {: 6.3f}".format(self.state["loss"]))
    print("TRAIN Time: {}".format(utils.time_diff_str(time1, time2)))

    """
      # the gt_soP_prior here is a subset of the 100*70*70 dimensional so_prior array, which contains the predicate prob distribution for all object pairs
      # the gt_soP_prior here contains the predicate probability distribution of only the object pairs in this annotation
      # Also, it might be helpful to keep in mind that this data layer currently works for a single annotation at a time - no batching is implemented!
      image_blob, boxes, rel_boxes, SpatialFea, classes, ix1, ix2, rel_labels, gt_soP_prior = self.datalayer...
    """

  def test_pre(self):
    recalls, dtime = self.eval.test_pre(self.model, self.eval_args.rec)
    print("PRED TEST:")
    print(self.get_format_str().format(*recalls))
    print("TEST Time: {}".format(utils.time_diff_str(dtime)))
    return recalls, dtime

  def test_rel(self):
    recalls, (pos_num, loc_num, gt_num), dtime = self.eval.test_rel(self.model, self.eval_args.rec)
    print("REL TEST:")
    print(self.get_format_str().format(*recalls))
    # print("OBJ TEST: POS: {: 6.3f}, LOC: {: 6.3f}, GT: {: 6.3f}, Precision: {: 6.3f}, Recall: {: 6.3f}".format(pos_num, loc_num, gt_num, np.float64(pos_num)/(pos_num+loc_num), np.float64(pos_num)/gt_num))
    print("TEST Time: {}".format(utils.time_diff_str(dtime)))
    return recalls, dtime

  def get_format_str(self):
    return "".join(["\t{}: {{: 6.3f}}".format(x) if i % 2 == 0 else "\t{}: {{: 6.3f}}\n".format(x) for i,x in enumerate(self.gt_headers())])

  def gt_headers(self, prefix="", test_type = True):
    def metric_name(x):
      if isinstance(x, float): return "{}x".format(x)
      elif isinstance(x, int): return str(x)
    if prefix == "":        fst_col_prefix = ""
    elif test_type != True: fst_col_prefix = "{} {} ".format(prefix, test_type)
    else:                   fst_col_prefix = "{} ".format(prefix)
    headers = []
    for i,x in enumerate(self.eval_args.rec):
      if i == 0: name = fst_col_prefix
      else:      name = ""
      name += "R@" + metric_name(x)
      headers.append(name)
      headers.append("ZS")
    return headers

if __name__ == "__main__":

  test_type = 0.1
  test_type = True

  # DEBUGGING: Test if code compiles
  if TEST_DEBUGGING:
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    trainer = vrd_trainer("test", {"training" : {"num_epochs" : 1}, "eval" : {"test_pre" : test_type,  "test_rel" : test_type},  "data" : {"justafew" : True}}, checkpoint="epoch_4_checkpoint.pth.tar")
    trainer.train()


  # TEST_VALIDITY: Test if a newly-introduced change affects the validity of the code
  if TEST_VALIDITY:
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    trainer = vrd_trainer("original-checkpoint", {"training" : {"num_epochs" : 1}, "eval" : {"test_pre" : test_type,  "test_rel" : test_type},  "data" : {"name" : "vrd/dsr"}}, checkpoint="epoch_4_checkpoint.pth.tar")
    trainer.train()
    trainer = vrd_trainer("original", {"training" : {"num_epochs" : 5}, "eval" : {"test_pre" : test_type,  "test_rel" : test_type},  "data" : {"name" : "vrd/dsr"}})
    trainer.train()

  # TEST_OVERFIT: Try overfitting the network to a single batch
  if TEST_OVERFIT:
    justafew = 3
    torch.backends.cudnn.deterministic = False
    torch.backends.cudnn.benchmark = True
    random.seed(datetime.now())
    np.random.seed(datetime.now())
    torch.manual_seed(datetime.now())
    args = {"training" : {"num_epochs" : 6}, "eval" : {"test_pre" : test_type,  "test_rel" : test_type, "justafew" : justafew},  "data" : {"justafew" : justafew}}
    #args = {"model" : {"use_pred_sem" : pred_sem_mode}, "training" : {"num_epochs" : 5, "opt": {"lr": lr, "weight_decay" : weight_decay, "lr_fus_ratio" : lr_rel_fus_ratio, "lr_rel_ratio" : lr_rel_fus_ratio}}}
    trainer = vrd_trainer("test-overfit", profile = "cfgs/pred_sem.yml")
    trainer.train()

  # Scan (rotating parameters)
  for lr in [0.0001]: # , 0.00001, 0.000001]: # [0.001, 0.0001, 0.00001, 0.000001]:
    for weight_decay in [0.0005]:
      for lr_rel_fus_ratio in [0.1, 1, 10]:
        for pred_sem_mode in [5,6,7,8,8+7,8+8]:
            #trainer = vrd_trainer("pred-sem-scan-v5-{}-{}-{}-{}".format(lr, weight_decay, lr_rel_fus_ratio, pred_sem_mode), {"model" : {"use_pred_sem" : pred_sem_mode}, "eval" : {"eval_obj":False, "test_rel":.2, "test_pre":.2}, "training" : {"num_epochs" : 5, "opt": {"lr": lr, "weight_decay" : weight_decay, "lr_fus_ratio" : lr_rel_fus_ratio, "lr_rel_ratio" : lr_rel_fus_ratio}}}, profile = "cfgs/pred_sem.yml", checkpoint = False)
            trainer = vrd_trainer("pred-sem-scan-v5-{}-{}-{}-{}".format(lr, weight_decay, lr_rel_fus_ratio, pred_sem_mode), {"model" : {"use_pred_sem" : pred_sem_mode}, "eval" : {"eval_obj":False, "test_rel":1., "test_pre":1.}, "training" : {"num_epochs" : 5, "opt": {"lr": lr, "weight_decay" : weight_decay, "lr_fus_ratio" : lr_rel_fus_ratio, "lr_rel_ratio" : lr_rel_fus_ratio}}}, profile = "cfgs/pred_sem.yml", checkpoint = False)
            #trainer = vrd_trainer("pred-sem-scan-v5-{}-{}-{}-{}".format(lr, weight_decay, lr_rel_fus_ratio, pred_sem_mode), {"model" : {"use_pred_sem" : pred_sem_mode}, "eval" : {"eval_obj":False, "test_rel":True, "test_pre":True}, "training" : {"num_epochs" : 5, "opt": {"lr": lr, "weight_decay" : weight_decay, "lr_fus_ratio" : lr_rel_fus_ratio, "lr_rel_ratio" : lr_rel_fus_ratio}}}, profile = "cfgs/pred_sem.yml", checkpoint = False)
            trainer.train()

  sys.exit(0)
