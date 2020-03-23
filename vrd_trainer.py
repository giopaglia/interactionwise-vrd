import os
import os.path as osp
import sys
import pickle
from tabulate import tabulate
import time

import torch
import torch.nn as nn
import torch.nn.init
import torch.nn.functional as F

from bunch import bunchify
import globals, utils
from lib.vrd_models import VRDModel
from lib.datalayers import VRDDataLayer
from evaluator import VRDEvaluator
#, save_net, load_net, \
#      adjust_learning_rate, , clip_gradient

# from lib.model import train_net, test_pre_net, test_rel_net


































class vrd_trainer():

  def __init__(self,
      checkpoint = False,
      args = {
        # Dataset in use
        "data" : {
          "name"      : "vrd",
          "with_bg_obj"  : False,
          "with_bg_pred" : False,
        },
        # Architecture (or model) type
        "model" : {
            # Constructor
            "type" : "DSRModel",
            # In addition to the visual features of the union box,
            #  use those of subject and object individually?
            "use_so" : True,

            # Use visual features
            "use_vis" : True,

            # Use semantic features (TODO: this becomes the size of the semantic features)
            "use_sem" : True,

            # Three types of spatial features:
            # - 0: no spatial info
            # - 1: 8-way relative location vector
            # - 2: dual mask
            "use_spat" : 0,

            # Size of the representation for each modality when fusing features
            "n_fus_neurons" : 256,

            # Use batch normalization or not
            "use_bn" : False,
          }
        ],
        # Evaluation Arguments
        "eval" : {
          "use_obj_prior" : True
        },
        # Training parameters
        "training" : {
          "opt" : {
            "lr"           : 0.00001,
            # "momentum"   : 0.9,
            "weight_decay" : 0.0005,
          },

          # Adjust learning rate every lr_decay_step epochs
          # TODO: check if this works:
          #"lr_decay_step"  : 3,
          #"lr_decay_gamma" : .1,

          "use_shuffle"    : True, # TODO: check if shuffle works

          "epoch" : 0,
          "num_epochs" : 20,
          "checkpoint_freq" : 5,

          # TODO make this such that -1 means "one full dataset round" (and maybe -2 is two full.., -3 is three but whatevs)
          "iters_per_epoch"  : -1,
          # Number of lines printed with loss ...TODO explain smart freq
          "prints_freq" : 10,

          # TODO
          "batch_size" : 1,

          "test_pre" : True,
          "test_rel" : False,
        }
      }):

    print("vrd_trainer() called with args:")
    print([checkpoint, args])

    self.session_name = "test-new-training"

    self.checkpoint = checkpoint
    self.args       = args

    self.state      = {}



    # Load checkpoint, if any
    if isinstance(self.checkpoint, str):

      checkpoint_path = osp.join(globals.models_dir, self.checkpoint)
      print("Loading checkpoint... ({})".format(checkpoint_path))

      if not osp.isfile(checkpoint_path):
        raise Exception("Checkpoint not found: {}".format(checkpoint_path))

      checkpoint = torch.load(checkpoint_path)

      # Session name
      utils.patch_key(checkpoint, "session", "session_name") # (patching)
      self.session_name = checkpoint["session_name"]

      # Arguments
      self.args = checkpoint["args"]

      if not checkpoint.get("epoch") is None:          # (patching)
        self.args["training"]["epoch"] = checkpoint["epoch"]


      # State
      # Model state dictionary
      utils.patch_key(checkpoint, "state_dict", ["state", "model_state_dict"]) # (patching)
      utils.patch_key(checkpoint["state"]["model_state_dict"], "fc_semantic.fc.weight", "fc_so_emb.fc.weight") # (patching)
      utils.patch_key(checkpoint["state"]["model_state_dict"], "fc_semantic.fc.bias",   "fc_so_emb.fc.bias") # (patching)
      # TODO: is this different than the weights used for initialization...? del checkpoint["model_state_dict"]["emb.weight"]
      # Optimizer state dictionary
      utils.patch_key(checkpoint, "optimizer", ["state", "optimizer_state_dict"]) # (patching)
      self.state = checkpoint["state"]

    # TODO: idea, don't use data_args.name but data.name?
    self.data_args    = bunchify(self.args["data"])
    self.model_args   = bunchify(self.args["model"])
    self.eval_args    = bunchify(self.args["eval"])
    self.training     = bunchify(self.args["training"])

    # Data
    print("Initializing data...")
    print("Data args: ", self.data_args)
    # TODO: VRDDataLayer has to know what to yield (DRS -> img_blob, obj_boxes, u_boxes, idx_s, idx_o, spatial_features, obj_classes)
    self.datalayer = VRDDataLayer(self.data_args, "train", use_shuffle)
    # TODO: Pytorch DataLoader instead:
    # self.num_workers = 0
    # self.dataset = VRDDataset()
    # self.datalayer = torch.utils.data.DataLoader(self.dataset,
    #                  batch_size=self.training.batch_size,
    #                  # sampler= Random ...,
    #                  num_workers=self.num_workers)


    # Model
    self.model_args.n_obj  = self.datalayer.n_obj
    self.model_args.n_pred = self.datalayer.n_pred
    print("Initializing VRD Model...")
    print("Model args: ", self.model_args)
    self.model = VRDModel(self.model_args).cuda()
    if "model_state_dict" in self.state:
      # TODO: Make sure that this doesn't need the random initialization first
      self.model.load_state_dict(self.state["model_state_dict"])
    else:
      # Random initialization
      utils.weights_normal_init(self.model, dev=0.01)
      # Load VGG layers
      self.model.load_pretrained_conv(osp.join(globals.data_dir, "VGG_imagenet.npy"), fix_layers=True)
      # Load existing (word2vec?) embeddings
      with open(osp.join(globals.data_dir, "vrd", "params_emb.pkl", 'rb') as f:
        self.model.state_dict()["emb.weight"].copy_(torch.from_numpy(pickle.load(f, encoding="latin1")))

    # Evaluation
    print("Initializing evaluation...")
    print("Evaluation args: ", self.eval_args)
    self.eval = VRDEvaluator(self.data_args, self.eval_args)

    # Training
    print("Initializing training...")
    print("Training args: ", self.training)
    self.optimizer = self.model.OriginalAdamOptimizer(**self.training.opt)
    # TODO: create loss_type argument...
    self.criterion = nn.MultiLabelMarginLoss().cuda()
    if "optimizer_state_dict" in self.state:
      self.optimizer.load_state_dict(self.state["optimizer_state_dict"])


  # Perform the complete training process
  def train(self):
    save_dir = osp.join(globals.models_dir, self.session_name)
    if not osp.exists(save_dir):
      os.mkdir(save_dir)
    save_file = osp.join(globals.models_dir, "{}.txt".format(self.session_name))

    # Prepare result table
    res_headers = ["Epoch"]
    if self.training.test_pre:
      res_headers += ["Pre R@50", "ZS", "R@100", "ZS"]
    if self.training.test_rel:
      res_headers += ["Rel R@50", "ZS", "R@100", "ZS"]
    res = []

    end_epoch = self.training.epoch + self.training.num_epochs
    while self.training.epoch < end_epoch:

      print("Epoch {}".format(self.training.epoch))


      # TODO check if this works (Note that you'd have to make it work cross-sessions as well)
      # if (self.training.epoch % (self.training.lr_decay_step + 1)) == 0:
      #   print("*adjust_learning_rate*")
      #   adjust_learning_rate(self.optimizer, self.training.lr_decay_gamma)
      # TODO do it with the scheduler, see if it's the same: https://pytorch.org/docs/stable/optim.html#how-to-adjust-learning-rate

      # self.__train_epoch(self.training.epoch)

      # Test results
      res_row = (self.training.epoch,)
      if self.training.test_pre:
        rec_50, rec_50_zs, rec_100, rec_100_zs, dtime = self.eval.test_pre(self.model)
        res_row += [rec_50, rec_50_zs, rec_100, rec_100_zs]
        print("CLS PRED TEST:\nAll:\tR@50: {: 6.3f}\tR@100: {: 6.3f}\nZShot:\tR@50: {: 6.3f}\tR@100: {: 6.3f}".format(rec_50, rec_100, rec_50_zs, rec_100_zs))
        print("TEST Time: {}".format(utils.time_diff_str(dtime)))
      if self.training.test_rel:
        rec_50, rec_50_zs, rec_100, rec_100_zs, pos_num, loc_num, gt_num, dtime = self.eval.test_rel(self.model)
        res_row += [rec_50, rec_50_zs, rec_100, rec_100_zs]
        print("CLS OBJ TEST POS: {: 6.3f}, LOC: {: 6.3f}, GT: {: 6.3f}, Precision: {: 6.3f}, Recall: {: 6.3f}".format(pos_num, loc_num, gt_num, pos_num/(pos_num+loc_num), pos_num/gt_num))
        print("CLS REL TEST:\nAll:\tR@50: {: 6.3f}\tR@100: {: 6.3f}\nZShot:\tR@50: {: 6.3f}\tR@100: {: 6.3f}".format(rec_50, rec_100, rec_50_zs, rec_100_zs))
        print("TEST Time: {}".format(utils.time_diff_str(dtime)))
      res.append(res_row)

      with open(save_file, 'w') as f:
        f.write(tabulate(res, res_headers))

      # Save checkpoint
      if utils.smart_fequency_check(self.training.epoch, self.training.num_epochs, self.training.checkpoint_freq):

        # TODO: the loss should be a result: self.result.loss (which is ignored at loading,only used when saving checkpoint)...
        self.state["model_state_dict"]     = self.model.state_dict()
        self.state["optimizer_state_dict"] = self.optimizer.state_dict()

        utils.save_checkpoint({
          "session_name"  : self.session_name,
          "args"          : self.args,
          "state"         : self.state,
          "result"        : dict(zip(res_headers, res_row)),
        }, osp.join(save_dir, "checkpoint_epoch_{}.pth.tar".format(self.training.epoch)))

      self.__train_epoch()

      self.training.epoch += 1

  def __train_epoch(self):
    self.model.train()

    time1 = time.time()
    # TODO check if LeveledAverageMeter works
    losses = utils.LeveledAverageMeter(2)

    # Iterate over the dataset
    n_iter = self.training.iters_per_epoch
    if n_iter < 0:
      # TODO: check that vrd during training ignores None images
      n_iter = self.datalayer.N * (-n_iter)

    for iter in range(n_iter):

      # Obtain next annotation input and target
      net_input, rel_sop_prior, target = self.datalayer.next()

      # TODO: is this necessary? If it's what I think, the datalayer should already ignore these
      if target.size()[1] == 0:
        continue

      # Forward pass & Backpropagation step
      self.optimizer.zero_grad()
      obj_scores, rel_scores = self.model(*net_input)

      # Preprocessing the rel_sop_prior before factoring it into the loss
      rel_sop_prior = torch.FloatTensor(rel_sop_prior).cuda()
      rel_sop_prior = -0.5 * ( rel_sop_prior + 1.0 / self.datalayer.n_pred)
      loss = self.criterion((rel_sop_prior + rel_scores).view(1, -1), target)
      # loss = self.criterion((rel_scores).view(1, -1), target)

      losses.update(loss.item())
      loss.backward()
      self.optimizer.step()

      # TODO: I'd like to move that thing here, but maybe I can't call item() after backward?
      # losses.update(loss.item())

      if utils.smart_fequency_check(step, n_iter, self.training.prints_per_epoch):
        print("\t{:4d}: LOSS: {: 6.3f}".format(step, losses.avg(0)))
        losses.reset(0)

    self.state["loss"] = losses.avg(1)
    time2 = time.time()

    print("TRAIN Loss: {: 6.3f}".format(self.state["loss"]))
    print("TRAIN Time: {}".format(utils.time_diff_str(time1, time2)))

    """
      # the rel_sop_prior here is a subset of the 100*70*70 dimensional so_prior array, which contains the predicate prob distribution for all object pairs
      # the rel_sop_prior here contains the predicate probability distribution of only the object pairs in this annotation
      # Also, it might be helpful to keep in mind that this data layer currently works for a single annotation at a time - no batching is implemented!
      image_blob, boxes, rel_boxes, SpatialFea, classes, ix1, ix2, rel_labels, rel_sop_prior = self.datalayer...
    """

if __name__ == "__main__":
  trainer = vrd_trainer()
  # trainer = vrd_trainer(checkpoint = False, self.data_args.name = "vrd")
  # trainer = vrd_trainer(checkpoint = "epoch_4_checkpoint.pth.tar")

  trainer.train()
